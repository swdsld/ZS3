import itertools
import math
import os

import numpy as np
import scipy.sparse as sp
import torch
from torch import nn
from tqdm import tqdm

from zs3.dataloaders import make_data_loader
from zs3.modeling.deeplab import DeepLab
from zs3.modeling.gmmn import GMMNnetwork_GCN, GMMNnetwork
from zs3.modeling.sync_batchnorm.replicate import patch_replication_callback
from zs3.utils.loss import SegmentationLosses, GMMNLoss
from zs3.utils.lr_scheduler import LR_Scheduler
from zs3.utils.metrics import Evaluator
from zs3.utils.saver import Saver
from zs3.utils.summaries import TensorboardSummary
from zs3.parsing import get_parser
from zs3.exp_data import CLASSES_NAMES


def sparse_mx_to_torch_sparse_tensor(sparse_mx):
    """Convert a scipy sparse matrix to a torch sparse tensor."""
    sparse_mx = sparse_mx.tocoo().astype(np.float32)
    indices = torch.from_numpy(np.vstack((sparse_mx.row, sparse_mx.col)).astype(np.int64))
    values = torch.from_numpy(sparse_mx.data)
    shape = torch.Size(sparse_mx.shape)
    return torch.sparse.FloatTensor(indices, values, shape)


def construct_adj_mat(segmap, embeddingmap, featmap, avg_feat=False):
    """ Represent each segmentation cluster by an unique cluster/node ID
        in: 2D numpy array of semantic segmentation map
        out: + adj_mat: torch sparse tensor: adjacency matrix of the semantic cluster graph
             + clsidx_2_pixidx: dictionary mapping from cluster ID to indices in the original segmap
             + clsidx_2_lbl: list mapping from cluster ID to semantic label
             + embedding_GCN: semantic embeddings of clusters
             + featmap: average features of clusters
    """
    row = []
    col = []
    data = []
    clsidx_2_pixidx = {}
    clsidx_2_lbl = []
    embedding_GCN = []
    feat_GCN = []
    dflag = {}
    N = segmap.shape[0]
    M = segmap.shape[1]
    flag = np.zeros((N, M)) - 1
    N_cluster = 0

    for (i, j) in itertools.product(range(N), range(M)):
        if flag[i][j] == -1:  # dfs
            N_cluster += 1
            embedding_GCN.append(embeddingmap[:, i, j])
            feat_cur = None
            cluster_lbl = segmap[i][j]
            clsidx_2_lbl.append(cluster_lbl)
            clsidx_2_pixidx[N_cluster - 1] = []
            stack = [(i, j)]
            cnt = 0
            while len(stack) > 0:
                cnt += 1
                (curi, curj) = stack.pop()
                clsidx_2_pixidx[N_cluster - 1].append((curi, curj))
                flag[curi][curj] = N_cluster - 1
                if feat_cur is None:
                    if featmap is not None:
                        feat_cur = featmap[:, i, j]
                else:
                    if featmap is not None and avg_feat:
                        feat_cur = (feat_cur * (cnt - 1) + featmap[:, i, j]) / cnt
                for (dx, dy) in itertools.product(range(-1, 2, 1), range(-1, 2, 1)):
                    (nebi, nebj) = (curi + dx, curj + dy)
                    if 0 <= nebi < N and 0 <= nebj < M:
                        if flag[nebi][nebj] == -1 and segmap[nebi][nebj] == cluster_lbl:
                            stack.append((nebi, nebj))
                        if flag[nebi][nebj] >= 0 and segmap[nebi][nebj] != cluster_lbl:
                            if not (flag[nebi][nebj], N_cluster - 1) in dflag:
                                row.append(N_cluster - 1)
                                col.append(flag[nebi][nebj])
                                data.append(1.0)
                                col.append(N_cluster - 1)
                                row.append(flag[nebi][nebj])
                                data.append(1.0)
                                dflag[(flag[nebi][nebj], N_cluster - 1)] = "true"
            if feat_cur is not None:
                feat_GCN.append(feat_cur)

    if N_cluster > 1:
        adj_mat = sp.coo_matrix((data, (row, col)), shape=(N_cluster, N_cluster))
        adj_mat = sparse_mx_to_torch_sparse_tensor(adj_mat)
    else:
        adj_mat = None

    embedding_GCN = np.vstack(embedding_GCN)
    if len(feat_GCN) > 0:
        feat_GCN = np.vstack(feat_GCN)
    return adj_mat, clsidx_2_pixidx, clsidx_2_lbl, embedding_GCN, feat_GCN


class Trainer:
    def __init__(self, args):
        self.args = args

        # Define Saver
        self.saver = Saver(args)
        self.saver.save_experiment_config()
        # Define Tensorboard Summary
        self.summary = TensorboardSummary(self.saver.experiment_dir)
        self.writer = self.summary.create_summary()

        # Define Dataloader
        kwargs = {"num_workers": args.workers, "pin_memory": True}
        (self.train_loader, self.val_loader, _, self.nclass,) = make_data_loader(
            args=args,
            load_embedding=args.load_embedding,
            w2c_size=args.w2c_size,
            **kwargs,
        )

        model = DeepLab(
            num_classes=self.nclass,
            output_stride=args.out_stride,
            sync_bn=args.sync_bn,
            freeze_bn=args.freeze_bn,
            global_avg_pool_bn=args.global_avg_pool_bn,
            imagenet_pretrained_path=args.imagenet_pretrained_path,
        )

        train_params = [
            {"params": model.get_1x_lr_params(), "lr": args.lr},
            {"params": model.get_10x_lr_params(), "lr": args.lr * 10},
        ]

        # Define Optimizer
        optimizer = torch.optim.SGD(
            train_params,
            momentum=args.momentum,
            weight_decay=args.weight_decay,
            nesterov=args.nesterov,
        )

        # Define Generator
        generator = GMMNnetwork(
            args.noise_dim,
            args.embed_dim,
            args.hidden_size,
            args.feature_dim,
            semantic_reconstruction=args.semantic_reconstruction,
        )
        optimizer_generator = torch.optim.Adam(
            generator.parameters(), lr=args.lr_generator
        )
        generator_GCN = GMMNnetwork_GCN(
            args.noise_dim, args.embed_dim, args.hidden_size, args.feature_dim
        )
        optimizer_generator_GCN = torch.optim.Adam(
            generator_GCN.parameters(), lr=args.lr_generator
        )

        class_weight = torch.ones(self.nclass)
        class_weight[args.unseen_classes_idx_metric] = args.unseen_weight
        if args.cuda:
            class_weight = class_weight.cuda()

        self.criterion = SegmentationLosses(
            weight=class_weight, cuda=args.cuda
        ).build_loss(mode=args.loss_type)
        self.model, self.optimizer = model, optimizer

        self.criterion_generator = GMMNLoss(
            sigma=[2, 5, 10, 20, 40, 80], cuda=args.cuda
        ).build_loss()

        self.generator, self.optimizer_generator = generator, optimizer_generator
        self.generator_GCN, self.optimizer_generator_GCN = (
            generator_GCN,
            optimizer_generator_GCN,
        )

        # Define Evaluator
        self.evaluator = Evaluator(
            self.nclass, args.seen_classes_idx_metric, args.unseen_classes_idx_metric
        )

        # Define lr scheduler
        self.scheduler = LR_Scheduler(
            args.lr_scheduler, args.lr, args.epochs, len(self.train_loader)
        )

        # Using cuda
        if args.cuda:
            self.model = torch.nn.DataParallel(self.model, device_ids=self.args.gpu_ids)
            patch_replication_callback(self.model)
            self.model = self.model.cuda()
            self.generator = self.generator.cuda()
            self.generator_GCN = self.generator_GCN.cuda()

        # Resuming checkpoint
        self.best_pred = 0.0
        if args.resume is not None:
            if not os.path.isfile(args.resume):
                raise RuntimeError(f"=> no checkpoint found at '{args.resume}'")
            checkpoint = torch.load(args.resume)
            # args.start_epoch = checkpoint['epoch']

            if args.random_last_layer:
                checkpoint["state_dict"]["decoder.pred_conv.weight"] = torch.rand(
                    (
                        self.nclass,
                        checkpoint["state_dict"]["decoder.pred_conv.weight"].shape[1],
                        checkpoint["state_dict"]["decoder.pred_conv.weight"].shape[2],
                        checkpoint["state_dict"]["decoder.pred_conv.weight"].shape[3],
                    )
                )
                checkpoint["state_dict"]["decoder.pred_conv.bias"] = torch.rand(
                    self.nclass
                )

            if args.cuda:
                self.model.module.load_state_dict(checkpoint["state_dict"])
            else:
                self.model.load_state_dict(checkpoint["state_dict"])

            if not args.ft:
                if not args.nonlinear_last_layer and not args.random_last_layer:
                    self.optimizer.load_state_dict(checkpoint["optimizer"])
            # self.best_pred = checkpoint['best_pred']
            print(f"=> loaded checkpoint '{args.resume}' (epoch {checkpoint['epoch']})")

        # Clear start epoch if fine-tuning
        if args.ft:
            args.start_epoch = 0

    def training(self, epoch, args):
        train_loss = 0.0
        self.model.train()
        tbar = tqdm(self.train_loader)
        num_img_tr = len(self.train_loader)
        for i, sample in enumerate(tbar):
            if len(sample["image"]) > 1:
                image, target, embedding = (
                    sample["image"],
                    sample["label"],
                    sample["label_emb"],
                )
                if self.args.cuda:
                    image, target, embedding = (
                        image.cuda(),
                        target.cuda(),
                        embedding.cuda(),
                    )
                self.scheduler(self.optimizer, i, epoch, self.best_pred)
                # ===================real feature extraction=====================
                with torch.no_grad():
                    real_features = self.model.module.forward_before_class_prediction(
                        image
                    )

                # ===================fake feature generation=====================
                fake_features = torch.zeros(real_features.shape)
                if args.cuda:
                    fake_features = fake_features.cuda()
                generator_loss_batch = 0.0
                generator_GCN_loss_batch = 0.0
                semantic_reconstruction_batch = 0.0
                fake_features_GCN = []
                target_GCN = []
                for (
                    count_sample_i,
                    (real_features_i, target_i, embedding_i),
                ) in enumerate(zip(real_features, target, embedding)):
                    generator_loss_sample = 0.0
                    semantic_reconstruction_loss_sample = 0.0

                    ## reduce to real feature size
                    real_features_i_ = real_features_i.permute(1, 2, 0).contiguous()
                    real_features_i = real_features_i_.view((-1, args.feature_dim))
                    target_i_ = nn.functional.interpolate(
                        target_i.view(1, 1, target_i.shape[0], target_i.shape[1]),
                        size=(real_features.shape[2], real_features.shape[3]),
                        mode="nearest",
                    )
                    target_i = target_i_.view(-1)
                    embedding_i = nn.functional.interpolate(
                        embedding_i.view(
                            1,
                            embedding_i.shape[0],
                            embedding_i.shape[1],
                            embedding_i.shape[2],
                        ),
                        size=(real_features.shape[2], real_features.shape[3]),
                        mode="nearest",
                    )

                    unique_class = torch.unique(target_i)
                    ## test if image has unseen class pixel, if yes means no training for generator and generated features for the whole image
                    has_unseen_class = False
                    for u_class in unique_class:
                        if u_class in args.unseen_classes_idx_metric:
                            has_unseen_class = True

                    ## construct adjacent matrix
                    (
                        adj_mat,
                        clsidx_2_pixidx,
                        clsidx_2_lbl,
                        embedding_GCN_i,
                        real_features_GCN_i,
                    ) = construct_adj_mat(
                        target_i_.data.cpu().numpy().squeeze(),
                        embedding_i.data.cpu().numpy().squeeze(),
                        real_features_i_.data.cpu()
                        .numpy()
                        .squeeze()
                        .transpose(2, 0, 1),
                        avg_feat=args.GCN_avg_feat,
                    )
                    if adj_mat is not None:
                        target_GCN = target_GCN + clsidx_2_lbl


                    embedding_i = (
                        embedding_i.permute(0, 2, 3, 1)
                        .contiguous()
                        .view((-1, args.embed_dim))
                    )

                    fake_features_i = torch.zeros(real_features_i.shape)
                    if args.cuda:
                        fake_features_i = fake_features_i.cuda()

                    # normal generator
                    for idx_in in unique_class:
                        if idx_in != 255:
                            self.optimizer_generator.zero_grad()
                            idx_class = target_i == idx_in
                            real_features_class = real_features_i[idx_class]
                            embedding_class = embedding_i[idx_class]

                            if args.context_aware:
                                z = torch.mean(
                                    embedding_i[target_i != 255], dim=0
                                ).repeat(embedding_class.shape[0], 1)
                            else:
                                z = torch.rand((embedding_class.shape[0], args.noise_dim))

                            if args.cuda:
                                z = z.cuda()

                            fake_features_class = self.generator(
                                embedding_class, z.float()
                            )

                            if (
                                idx_in in args.seen_classes_idx_metric
                                and not has_unseen_class
                            ):
                                ## in order to avoid CUDA out of memory
                                random_idx = torch.randint(
                                    low=0,
                                    high=fake_features_class.shape[0],
                                    size=(args.batch_size_generator,),
                                )
                                g_loss = self.criterion_generator(
                                    fake_features_class[random_idx],
                                    real_features_class[random_idx],
                                )
                                generator_loss_sample += g_loss.item()
                                g_loss.backward()
                                self.optimizer_generator.step()

                            fake_features_i[idx_class] = fake_features_class.clone()
                    generator_loss_batch += generator_loss_sample / len(unique_class)
                    semantic_reconstruction_batch += (
                        semantic_reconstruction_loss_sample / len(unique_class)
                    )
                    if args.real_seen_features and not has_unseen_class:
                        fake_features[count_sample_i] = real_features_i.view(
                            (
                                fake_features.shape[2],
                                fake_features.shape[3],
                                args.feature_dim,
                            )
                        ).permute(2, 0, 1)
                    else:
                        fake_features[count_sample_i] = fake_features_i.view(
                            (
                                fake_features.shape[2],
                                fake_features.shape[3],
                                args.feature_dim,
                            )
                        ).permute(2, 0, 1)


                    # GCN generator
                    if adj_mat is not None:
                        self.optimizer_generator_GCN.zero_grad()
                        embedding_GCN_i_pt = torch.FloatTensor(embedding_GCN_i).cuda()
                        z_GCN = torch.rand((embedding_GCN_i.shape[0], args.noise_dim))
                        if args.cuda:
                            z_GCN = z_GCN.cuda()
                        fake_features_GCN_i = self.generator_GCN(
                            embedding_GCN_i_pt, z_GCN.float(), adj_mat.cuda()
                        )
                        real_features_GCN_i = torch.FloatTensor(
                            real_features_GCN_i
                        ).cuda()
                        if not has_unseen_class:
                            g_GCN_loss = self.criterion_generator(
                                fake_features_GCN_i, real_features_GCN_i
                            )
                            g_GCN_loss.backward()
                            self.optimizer_generator_GCN.step()
                            generator_GCN_loss_batch += g_GCN_loss.item()

                        if args.real_seen_features and not has_unseen_class:
                            fake_features_GCN.append(
                                real_features_GCN_i.detach().data.cpu().numpy()
                            )
                        else:
                            fake_features_GCN.append(
                                fake_features_GCN_i.detach().data.cpu().numpy()
                            )


                # ===================classification=====================
                self.optimizer.zero_grad()
                output = self.model.module.forward_class_prediction(
                    fake_features.detach(), image.size()[2:]
                )
                loss = self.criterion(output, target)
                loss.backward()
                # GCN
                if len(fake_features_GCN) > 0:
                    fake_features_GCN = np.vstack(fake_features_GCN).transpose(1, 0)
                    fake_features_GCN_pt = torch.unsqueeze(
                        torch.unsqueeze(torch.FloatTensor(fake_features_GCN), 2), 0
                    ).cuda()
                    output_GCN = self.model.module.decoder.forward_class_prediction(
                        fake_features_GCN_pt
                    )
                    target_GCN_pt = torch.unsqueeze(
                        torch.unsqueeze(torch.FloatTensor(np.array(target_GCN)), 1),
                        0,
                    ).cuda()
                    loss_GCN = args.GCN_weight * self.criterion(
                        output_GCN, target_GCN_pt
                    )
                    loss_GCN.backward()

                self.optimizer.step()
                train_loss += loss.item()

                # ===================log=====================
                tbar.set_description(
                    f" G loss: {generator_loss_batch:.3f}"
                    + " C loss: %.3f" % (train_loss / (i + 1))
                )
                self.writer.add_scalar(
                    "train/total_loss_iter", loss.item(), i + num_img_tr * epoch
                )
                self.writer.add_scalar(
                    "train/generator_loss", generator_loss_batch, i + num_img_tr * epoch
                )
                self.writer.add_scalar(
                    "train/generator_GCN_loss",
                    generator_GCN_loss_batch,
                    i + num_img_tr * epoch,
                )
                self.writer.add_scalar(
                    "train/semantic_reconstruction_loss",
                    semantic_reconstruction_batch,
                    i + num_img_tr * epoch,
                )

                # Show 10 * 3 inference results each epoch
                if i % (num_img_tr // 10) == 0:
                    global_step = i + num_img_tr * epoch
                    self.summary.visualize_image(
                        self.writer,
                        self.args.dataset,
                        image,
                        target,
                        output,
                        global_step,
                    )

        self.writer.add_scalar("train/total_loss_epoch", train_loss, epoch)
        print(
            "[Epoch: %d, numImages: %5d]"
            % (epoch, i * self.args.batch_size + image.data.shape[0])
        )
        print(f"Loss: {train_loss:.3f}")

        if self.args.no_val:
            # save checkpoint every epoch
            is_best = False
            self.saver.save_checkpoint(
                {
                    "epoch": epoch + 1,
                    "state_dict": self.model.module.state_dict(),
                    "optimizer": self.optimizer.state_dict(),
                    "best_pred": self.best_pred,
                },
                is_best,
            )

    def validation(self, epoch, args):
        self.model.eval()
        self.evaluator.reset()
        tbar = tqdm(self.val_loader, desc="\r")
        test_loss = 0.0

        saved_images = {}
        saved_target = {}
        saved_prediction = {}
        for idx_unseen_class in args.unseen_classes_idx_metric:
            saved_images[idx_unseen_class] = []
            saved_target[idx_unseen_class] = []
            saved_prediction[idx_unseen_class] = []

        for i, sample in enumerate(tbar):
            image, target, embedding = (
                sample["image"],
                sample["label"],
                sample["label_emb"],
            )
            if self.args.cuda:
                image, target = image.cuda(), target.cuda()
            with torch.no_grad():
                if args.nonlinear_last_layer:
                    output = self.model(image, image.size()[2:])
                else:
                    output = self.model(image)
            loss = self.criterion(output, target)
            test_loss += loss.item()
            tbar.set_description("Test loss: %.3f" % (test_loss / (i + 1)))
            ## save image for tensorboard
            for idx_unseen_class in args.unseen_classes_idx_metric:
                if len((target.reshape(-1) == idx_unseen_class).nonzero()) > 0:
                    if len(saved_images[idx_unseen_class]) < args.saved_validation_images:
                        saved_images[idx_unseen_class].append(image.clone().cpu())
                        saved_target[idx_unseen_class].append(target.clone().cpu())
                        saved_prediction[idx_unseen_class].append(output.clone().cpu())

            pred = output.data.cpu().numpy()
            target = target.cpu().numpy()
            pred = np.argmax(pred, axis=1)
            # Add batch sample into evaluator
            self.evaluator.add_batch(target, pred)

        # Fast test during the training
        Acc, Acc_seen, Acc_unseen = self.evaluator.Pixel_Accuracy()
        (
            Acc_class,
            Acc_class_by_class,
            Acc_class_seen,
            Acc_class_unseen,
        ) = self.evaluator.Pixel_Accuracy_Class()
        (
            mIoU,
            mIoU_by_class,
            mIoU_seen,
            mIoU_unseen,
        ) = self.evaluator.Mean_Intersection_over_Union()
        (
            FWIoU,
            FWIoU_seen,
            FWIoU_unseen,
        ) = self.evaluator.Frequency_Weighted_Intersection_over_Union()
        self.writer.add_scalar("val_overall/total_loss_epoch", test_loss, epoch)
        self.writer.add_scalar("val_overall/mIoU", mIoU, epoch)
        self.writer.add_scalar("val_overall/Acc", Acc, epoch)
        self.writer.add_scalar("val_overall/Acc_class", Acc_class, epoch)
        self.writer.add_scalar("val_overall/fwIoU", FWIoU, epoch)

        self.writer.add_scalar("val_seen/mIoU", mIoU_seen, epoch)
        self.writer.add_scalar("val_seen/Acc", Acc_seen, epoch)
        self.writer.add_scalar("val_seen/Acc_class", Acc_class_seen, epoch)
        self.writer.add_scalar("val_seen/fwIoU", FWIoU_seen, epoch)

        self.writer.add_scalar("val_unseen/mIoU", mIoU_unseen, epoch)
        self.writer.add_scalar("val_unseen/Acc", Acc_unseen, epoch)
        self.writer.add_scalar("val_unseen/Acc_class", Acc_class_unseen, epoch)
        self.writer.add_scalar("val_unseen/fwIoU", FWIoU_unseen, epoch)

        print("Validation:")
        print(
            "[Epoch: %d, numImages: %5d]"
            % (epoch, i * self.args.batch_size + image.data.shape[0])
        )
        print(f"Loss: {test_loss:.3f}")
        print(f"Overall: Acc:{Acc}, Acc_class:{Acc_class}, mIoU:{mIoU}, fwIoU: {FWIoU}")
        print(
            "Seen: Acc:{}, Acc_class:{}, mIoU:{}, fwIoU: {}".format(
                Acc_seen, Acc_class_seen, mIoU_seen, FWIoU_seen
            )
        )
        print(
            "Unseen: Acc:{}, Acc_class:{}, mIoU:{}, fwIoU: {}".format(
                Acc_unseen, Acc_class_unseen, mIoU_unseen, FWIoU_unseen
            )
        )

        for class_name, acc_value, mIoU_value in zip(
            CLASSES_NAMES, Acc_class_by_class, mIoU_by_class
        ):
            self.writer.add_scalar("Acc_by_class/" + class_name, acc_value, epoch)
            self.writer.add_scalar("mIoU_by_class/" + class_name, mIoU_value, epoch)
            print(class_name, "- acc:", acc_value, " mIoU:", mIoU_value)

        new_pred = mIoU_unseen

        is_best = True
        self.best_pred = new_pred
        self.saver.save_checkpoint(
            {
                "epoch": epoch + 1,
                "state_dict": self.model.module.state_dict(),
                "optimizer": self.optimizer.state_dict(),
                "best_pred": self.best_pred,
            },
            is_best,
            generator_state={
                "epoch": epoch + 1,
                "state_dict": self.generator.state_dict(),
                "state_dict_GCN": self.generator_GCN.state_dict(),
                "optimizer": self.optimizer_generator.state_dict(),
                "optimizer_GCN": self.optimizer_generator_GCN.state_dict(),
                "best_pred": self.best_pred,
            },
        )

        global_step = epoch + 1
        for idx_unseen_class in args.unseen_classes_idx_metric:
            if len(saved_images[idx_unseen_class]) > 0:
                nb_image = len(saved_images[idx_unseen_class])
                if nb_image > args.saved_validation_images:
                    nb_image = args.saved_validation_images
                for i in range(nb_image):
                    self.summary.visualize_image_validation(
                        self.writer,
                        self.args.dataset,
                        saved_images[idx_unseen_class][i],
                        saved_target[idx_unseen_class][i],
                        saved_prediction[idx_unseen_class][i],
                        global_step,
                        name="validation_"
                        + CLASSES_NAMES[idx_unseen_class]
                        + "_"
                        + str(i),
                        nb_image=1,
                    )

        self.evaluator.reset()


def main():
    parser = get_parser()
    parser.add_argument(
        "--out-stride", type=int, default=16, help="network output stride (default: 8)"
    )

    # PASCAL VOC
    parser.add_argument(
        "--dataset",
        type=str,
        default="context",
        choices=["pascal", "coco", "cityscapes"],
        help="dataset name (default: pascal)",
    )

    parser.add_argument(
        "--use-sbd",
        action="store_true",
        default=True,
        help="whether to use SBD dataset (default: True)",
    )
    parser.add_argument("--base-size", type=int, default=312, help="base image size")
    parser.add_argument("--crop-size", type=int, default=312, help="crop image size")
    parser.add_argument(
        "--loss-type",
        type=str,
        default="ce",
        choices=["ce", "focal"],
        help="loss func type (default: ce)",
    )
    # training hyper params

    # PASCAL VOC
    parser.add_argument(
        "--epochs",
        type=int,
        default=100,
        metavar="N",
        help="number of epochs to train (default: auto)",
    )

    # PASCAL VOC
    parser.add_argument(
        "--batch-size",
        type=int,
        default=8,
        metavar="N",
        help="input batch size for training (default: auto)",
    )
    # false if embedding resume
    parser.add_argument("--global_avg_pool_bn", type=bool, default=True)

    # evaluation option
    parser.add_argument(
        "--eval-interval", type=int, default=1, help="evaluation interval (default: 1)"
    )

    # keep empty
    parser.add_argument("--unseen_classes_idx", type=int, default=[])  # not used

    # 2 unseen
    unseen_names = ["cow", "motorbike"]
    # 4 unseen
    # unseen_names = ['cow', 'motorbike', 'sofa', 'cat']
    # 6 unseen
    # unseen_names = ['cow', 'motorbike', 'sofa', 'cat', 'boat', 'fence']
    # 8 unseen
    # unseen_names = ['cow', 'motorbike', 'sofa', 'cat', 'boat', 'fence', 'bird', 'tvmonitor']
    # 10 unseen
    # unseen_names = ['cow', 'motorbike', 'sofa', 'cat', 'boat', 'fence', 'bird', 'tvmonitor', 'aeroplane', 'keyboard']

    unseen_classes_idx_metric = []
    for name in unseen_names:
        unseen_classes_idx_metric.append(CLASSES_NAMES.index(name))

    ### FOR METRIC COMPUTATION IN ORDER TO GET PERFORMANCES FOR TWO SETS
    seen_classes_idx_metric = np.arange(60)

    seen_classes_idx_metric = np.delete(
        seen_classes_idx_metric, unseen_classes_idx_metric
    ).tolist()
    parser.add_argument(
        "--seen_classes_idx_metric", type=int, default=seen_classes_idx_metric
    )
    parser.add_argument(
        "--unseen_classes_idx_metric", type=int, default=unseen_classes_idx_metric
    )

    parser.add_argument(
        "--unseen_weight", type=int, default=100, help="number of output channels"
    )

    parser.add_argument(
        "--nonlinear_last_layer", type=bool, default=False, help="non linear prediction"
    )
    parser.add_argument(
        "--random_last_layer", type=bool, default=True, help="randomly init last layer"
    )

    parser.add_argument(
        "--real_seen_features",
        type=bool,
        default=True,
        help="real features for seen classes",
    )
    parser.add_argument(
        "--load_embedding",
        type=str,
        default="my_w2c",
        choices=["attributes", "w2c", "w2c_bg", "my_w2c", "fusion", None],
    )
    parser.add_argument("--w2c_size", type=int, default=300)

    ### GENERATOR ARGS
    parser.add_argument("--noise_dim", type=int, default=300)
    parser.add_argument("--embed_dim", type=int, default=300)
    parser.add_argument("--hidden_size", type=int, default=256)
    parser.add_argument("--feature_dim", type=int, default=256)
    parser.add_argument("--lr_generator", type=float, default=0.0002)
    parser.add_argument("--batch_size_generator", type=int, default=128)
    parser.add_argument("--saved_validation_images", type=int, default=10)

    parser.add_argument(
        "--semantic_reconstruction",
        type=bool,
        default=False,
        help="semantic_reconstruction after feature generation",
    )
    parser.add_argument("--lbd_sr", type=float, default=0.0001)

    parser.add_argument("--context_aware", type=bool, default=False)

    # GCN
    parser.add_argument("--context_GCN_aware", type=bool, default=True)
    parser.add_argument(
        "--GCN_avg_feat", action="store_true", help="whether using avg feat for GCN"
    )
    parser.add_argument(
        "--GCN_weight", type=float, default=0.1, help="GCN context weight"
    )

    parser.add_argument(
        "--imagenet_pretrained_path",
        type=str,
        default="checkpoint/resnet_backbone_pretrained_imagenet_wo_pascalcontext.pth.tar",
    )

    parser.add_argument(
        "--resume",
        type=str,
        default="checkpoint/deeplab_pretrained_pascal_context_02_unseen.pth.tar",
        help="put the path to resuming file if needed",
    )

    parser.add_argument(
        "--checkname",
        type=str,
        default="gmmn_context_w2c300_linear_weighted100_hs256_2_unseen_withGCNcontext_w0_1",
    )

    args = parser.parse_args()
    args.cuda = not args.no_cuda and torch.cuda.is_available()
    if args.cuda:
        try:
            args.gpu_ids = [int(s) for s in args.gpu_ids.split(",")]
        except ValueError:
            raise ValueError(
                "Argument --gpu_ids must be a comma-separated list of integers only"
            )

    args.sync_bn = args.cuda and len(args.gpu_ids) > 1

    # default settings for epochs, batch_size and lr
    if args.epochs is None:
        epoches = {
            "coco": 30,
            "cityscapes": 200,
            "pascal": 50,
        }
        args.epochs = epoches[args.dataset.lower()]

    if args.batch_size is None:
        args.batch_size = 4 * len(args.gpu_ids)

    if args.test_batch_size is None:
        args.test_batch_size = args.batch_size

    if args.lr is None:
        lrs = {
            "coco": 0.1,
            "cityscapes": 0.01,
            "pascal": 0.007,
        }
        args.lr = lrs[args.dataset.lower()] / (4 * len(args.gpu_ids)) * args.batch_size

    if args.checkname is None:
        args.checkname = "deeplab-resnet"
    if args.context_GCN_aware:
        if args.GCN_avg_feat:
            args.checkname += "_avgfeat"
        if args.GCN_weight != 1.0:
            args.checkname += str(args.GCN_weight)
    print(args)
    print(args.checkname)
    torch.manual_seed(args.seed)
    trainer = Trainer(args)
    print("Starting Epoch:", trainer.args.start_epoch)
    print("Total Epoches:", trainer.args.epochs)
    for epoch in range(trainer.args.start_epoch, trainer.args.epochs):
        trainer.training(epoch, args)
        if not trainer.args.no_val and epoch % args.eval_interval == (
            args.eval_interval - 1
        ):
            trainer.validation(epoch, args)

    trainer.writer.close()


if __name__ == "__main__":
    main()

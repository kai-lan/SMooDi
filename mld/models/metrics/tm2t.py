from typing import List

import torch
from torch import Tensor
from torchmetrics import Metric
from torchmetrics.functional import pairwise_euclidean_distance
# from tmr.src.model.tmr import get_score_matrix
from .utils import *
from scipy.ndimage import uniform_filter1d


def calculate_skating_ratio(motions):
    thresh_height = 0.05 # 10
    fps = 20.0
    thresh_vel = 0.50 # 20 cm /s
    avg_window = 5 # frames

    batch_size = motions.shape[0]
    # 10 left, 11 right foot. XZ plane, y up
    # motions [bs, 22, 3, max_len]
    verts_feet = motions[:, [10, 11], :, :].detach().cpu().numpy()  # [bs, 2, 3, max_len]
    verts_feet_plane_vel = np.linalg.norm(verts_feet[:, :, [0, 2], 1:] - verts_feet[:, :, [0, 2], :-1],  axis=2) * fps  # [bs, 2, max_len-1]
    # [bs, 2, max_len-1]
    vel_avg = uniform_filter1d(verts_feet_plane_vel, axis=-1, size=avg_window, mode='constant', origin=0)

    verts_feet_height = verts_feet[:, :, 1, :]  # [bs, 2, max_len]
    # If feet touch ground in agjecent frames
    feet_contact = np.logical_and((verts_feet_height[:, :, :-1] < thresh_height), (verts_feet_height[:, :, 1:] < thresh_height))  # [bs, 2, max_len - 1]
    # skate velocity
    skate_vel = feet_contact * vel_avg

    # it must both skating in the current frame
    skating = np.logical_and(feet_contact, (verts_feet_plane_vel > thresh_vel))
    # and also skate in the windows of frames
    skating = np.logical_and(skating, (vel_avg > thresh_vel))

    # Both feet slide
    skating = np.logical_or(skating[:, 0, :], skating[:, 1, :]) # [bs, max_len -1]
    skating_ratio = np.sum(skating, axis=1) / skating.shape[1]

    return skating_ratio, skate_vel


def topk_accuracy(outputs, labels, topk=(1,3,5)):
    """
    Compute the top-k accuracy for the given outputs and labels.

    :param outputs: Tensor of model outputs, shape [batch_size, num_classes]
    :param labels: Tensor of labels, shape [batch_size]
    :param topk: Tuple of k values for which to compute top-k accuracy
    :return: List of top-k accuracies for each k in topk
    """
    maxk = max(topk)

    batch_size = labels.size(0)
    outputs = outputs.squeeze()
    # Get the top maxk indices along the last dimension (num_classes)
    _, pred = outputs.topk(maxk, 1, True, True)

    pred = pred.t()

    # Check if the labels are in the top maxk predictions
    correct = pred.eq(labels.view(1, -1).expand_as(pred))

    # Compute accuracy for each k
    accuracies = []
    for k in topk:
        correct_k = correct[:k].reshape(-1).float().sum(0, keepdim=True)
        accuracies.append(correct_k.mul_(100.0 / batch_size))
    return accuracies

class TM2TMetrics(Metric):
    full_state_update = True

    def __init__(self,
                 top_k=3,
                 R_size=32,
                 diversity_times=3,
                 dist_sync_on_step=True,
                 **kwargs):
        super().__init__(dist_sync_on_step=dist_sync_on_step)

        self.name = "matching, fid, and diversity scores"

        self.top_k = top_k
        self.R_size = R_size
        self.diversity_times = diversity_times

        self.add_state("count", default=torch.tensor(0), dist_reduce_fx="sum")
        self.add_state("count_seq",
                       default=torch.tensor(0),
                       dist_reduce_fx="sum")

        self.metrics = []
        # Matching scores
        self.add_state("Matching_score",
                       default=torch.tensor(0.0),
                       dist_reduce_fx="sum")
        self.add_state("gt_Matching_score",
                       default=torch.tensor(0.0),
                       dist_reduce_fx="sum")

        self.Matching_metrics = ["Matching_score", "gt_Matching_score"]
        for k in range(1, top_k + 1):
            self.add_state(
                f"R_precision_top_{str(k)}",
                default=torch.tensor(0.0),
                dist_reduce_fx="sum",
            )
            self.Matching_metrics.append(f"R_precision_top_{str(k)}")
        for k in range(1, top_k + 1):
            self.add_state(
                f"gt_R_precision_top_{str(k)}",
                default=torch.tensor(0.0),
                dist_reduce_fx="sum",
            )
            self.Matching_metrics.append(f"gt_R_precision_top_{str(k)}")

        self.metrics.extend(self.Matching_metrics)

        # Fid
        self.add_state("FID", default=torch.tensor(0.0), dist_reduce_fx="sum")
        self.metrics.append("FID")

        # SRA
        self.add_state("SRA", default=torch.tensor(0.0), dist_reduce_fx="sum")
        self.metrics.append("SRA")
        self.add_state("SRA_3", default=torch.tensor(0.0), dist_reduce_fx="sum")
        self.metrics.append("SRA_3")
        self.add_state("SRA_5", default=torch.tensor(0.0), dist_reduce_fx="sum")
        self.metrics.append("SRA_5")

        #skate_ratio
        self.add_state("skate_ratio", default=torch.tensor(0.0), dist_reduce_fx="sum")
        self.metrics.append("skate_ratio")


        #inference_time
        self.add_state("Inference_time", default=torch.tensor(0.0), dist_reduce_fx="sum")
        self.metrics.append("Inference_time")

        #tmr for text-motion similarity
        # self.add_state("Text-Sim", default=torch.tensor(0.0), dist_reduce_fx="sum")

        # Diversity
        self.add_state("Diversity",
                       default=torch.tensor(0.0),
                       dist_reduce_fx="sum")
        self.add_state("gt_Diversity",
                       default=torch.tensor(0.0),
                       dist_reduce_fx="sum")
        self.metrics.extend(["Diversity", "gt_Diversity"])

        # chached batches
        self.add_state("text_embeddings", default=[], dist_reduce_fx=None)
        self.add_state("recmotion_embeddings", default=[], dist_reduce_fx=None)
        self.add_state("gtmotion_embeddings", default=[], dist_reduce_fx=None)

        self.add_state("predicted", default=[], dist_reduce_fx=None)
        self.add_state("label", default=[], dist_reduce_fx=None)

        # self.add_state("lat_tmr_t", default=[], dist_reduce_fx=None)
        # self.add_state("lat_tmr_m", default=[], dist_reduce_fx=None)

        self.add_state("joints_rst", default=[], dist_reduce_fx=None)

        self.add_state("time", default=[], dist_reduce_fx=None)


    def compute(self, sanity_flag):
        count = self.count.item()
        count_seq = self.count_seq.item()

        # init metrics
        metrics = {metric: getattr(self, metric) for metric in self.metrics}

        # if in sanity check stage then jump
        if sanity_flag:
            return metrics

        # cat all embeddings
        shuffle_idx = torch.randperm(count_seq)
        all_texts = torch.cat(self.text_embeddings,
                              axis=0).cpu()[shuffle_idx, :]
        all_genmotions = torch.cat(self.recmotion_embeddings,
                                   axis=0).cpu()[shuffle_idx, :]
        all_gtmotions = torch.cat(self.gtmotion_embeddings,
                                  axis=0).cpu()[shuffle_idx, :]


        all_joints_rst = self.joints_rst

        all_predicted = torch.cat(self.predicted,
                              axis=0).cpu()[shuffle_idx, :]

        all_label = torch.cat(self.label,
                              axis=0).cpu()[shuffle_idx, :]


        all_time_list = self.time
        # Compute r-precision

        assert count_seq > self.R_size
        top_k_mat = torch.zeros((self.top_k, ))
        for i in range(count_seq // self.R_size):
            # [bs=32, 1*256]
            group_texts = all_texts[i * self.R_size:(i + 1) * self.R_size]
            # [bs=32, 1*256]
            group_motions = all_genmotions[i * self.R_size:(i + 1) *
                                           self.R_size]
            # dist_mat = pairwise_euclidean_distance(group_texts, group_motions)
            # [bs=32, 32]
            dist_mat = euclidean_distance_matrix(group_texts,
                                                 group_motions).nan_to_num()
            # print(dist_mat[:5])
            self.Matching_score += dist_mat.trace()
            argsmax = torch.argsort(dist_mat, dim=1)

            top_k_mat += calculate_top_k(argsmax, top_k=self.top_k).sum(axis=0)

        R_count = count_seq // self.R_size * self.R_size
        metrics["Matching_score"] = self.Matching_score / R_count
        for k in range(self.top_k):
            metrics[f"R_precision_top_{str(k+1)}"] = top_k_mat[k] / R_count

        # Compute r-precision with gt
        assert count_seq > self.R_size
        top_k_mat = torch.zeros((self.top_k, ))
        for i in range(count_seq // self.R_size):
            # [bs=32, 1*256]
            group_texts = all_texts[i * self.R_size:(i + 1) * self.R_size]
            # [bs=32, 1*256]
            group_motions = all_gtmotions[i * self.R_size:(i + 1) *
                                          self.R_size]
            # [bs=32, 32]
            dist_mat = euclidean_distance_matrix(group_texts,
                                                 group_motions).nan_to_num()
            # match score
            self.gt_Matching_score += dist_mat.trace()
            argsmax = torch.argsort(dist_mat, dim=1)
            top_k_mat += calculate_top_k(argsmax, top_k=self.top_k).sum(axis=0)
        metrics["gt_Matching_score"] = self.gt_Matching_score / R_count
        for k in range(self.top_k):
            metrics[f"gt_R_precision_top_{str(k+1)}"] = top_k_mat[k] / R_count

        # tensor -> numpy for FID
        all_genmotions = all_genmotions.numpy()
        all_gtmotions = all_gtmotions.numpy()

        # Compute fid
        mu, cov = calculate_activation_statistics_np(all_genmotions)
        # gt_mu, gt_cov = calculate_activation_statistics_np(all_gtmotions)
        gt_mu, gt_cov = calculate_activation_statistics_np(all_gtmotions)
        metrics["FID"] = calculate_frechet_distance_np(gt_mu, gt_cov, mu, cov)

        # Compute skate_ratio
        # motions [bs, 22, 3, max_len]
        skate_ratio_sum = 0.0
        for index in range(0,len(all_joints_rst)):
            # joints = all_joints_rst[index]
            skate_ratio, skate_vel = calculate_skating_ratio(all_joints_rst[index].permute(0, 2, 3, 1))
            skate_ratio_sum += skate_ratio

        # skate_ratio, skate_vel = calculate_skating_ratio(all_joints_rst.permute(0, 2, 3, 1))
        metrics["skate_ratio"] = skate_ratio_sum / len(all_joints_rst)

        # similarity_score = get_score_matrix(all_lat_tmr_t, all_lat_tmr_m).cpu()
        # metrics["Text-Sim"] = similarity_score.mean()
        output = topk_accuracy(all_predicted,all_label)
        metrics["SRA_3"] = output[1]
        metrics["SRA_5"] = output[2]
        metrics["SRA"] = output[0]#calculate_SRA(all_predicted.numpy(), all_label.numpy())


        #inference_time
        all_time = 0.0

        for index in range(0,len(all_time_list)):
            # joints = all_joints_rst[index]
            all_time += all_time_list[index]
        print("all_time",all_time)
        metrics["Inference_time"] = all_time.numpy() / len(all_time_list)


        # Compute diversity
        self.diversity_times = 20
        assert count_seq > self.diversity_times
        metrics["Diversity"] = calculate_diversity_np(all_genmotions,
                                                      self.diversity_times)
        metrics["gt_Diversity"] = calculate_diversity_np(
            all_gtmotions, self.diversity_times)

        return {**metrics}

    def update(
        self,
        text_embeddings: Tensor,
        recmotion_embeddings: Tensor,
        gtmotion_embeddings: Tensor,
        lengths: List[int],
        predicted: Tensor,
        label: Tensor,

        # lat_tmr_m: Tensor,
        # lat_tmr_t: Tensor,
        joints_rst: Tensor,
        time:Tensor,
    ):
        self.count += sum(lengths)
        self.count_seq += len(lengths)

        # [bs, nlatent*ndim] <= [bs, nlatent, ndim]
        text_embeddings = torch.flatten(text_embeddings, start_dim=1).detach()
        recmotion_embeddings = torch.flatten(recmotion_embeddings,
                                             start_dim=1).detach()
        gtmotion_embeddings = torch.flatten(gtmotion_embeddings,
                                            start_dim=1).detach()

        # lat_tmr_m = torch.flatten(lat_tmr_m.unsqueeze(0),
        #                                     start_dim=1).detach()

        # lat_tmr_t = torch.flatten(lat_tmr_t.unsqueeze(0),
        #                                     start_dim=1).detach()


        predicted = predicted.unsqueeze(0).unsqueeze(0)
        label = label.unsqueeze(0).unsqueeze(0)


        # store all texts and motions
        self.text_embeddings.append(text_embeddings)
        self.recmotion_embeddings.append(recmotion_embeddings)
        self.gtmotion_embeddings.append(gtmotion_embeddings)
        self.predicted.append(predicted)
        self.label.append(label)

        print("time:::",time)
        self.joints_rst.append(joints_rst)

        self.time.append(time)


class TM2TMetrics_Walk(Metric):
    full_state_update = True

    def __init__(self,
                 top_k=3,
                 R_size=32,
                 diversity_times=3,
                 dist_sync_on_step=True,
                 **kwargs):
        super().__init__(dist_sync_on_step=dist_sync_on_step)

        self.name = "matching, fid, and diversity scores"

        self.top_k = top_k
        self.R_size = R_size
        self.diversity_times = 80#diversity_times

        self.add_state("count", default=torch.tensor(0), dist_reduce_fx="sum")
        self.add_state("count_seq",
                       default=torch.tensor(0),
                       dist_reduce_fx="sum")

        self.metrics = []

        # Fid
        self.add_state("FID", default=torch.tensor(0.0), dist_reduce_fx="sum")
        self.metrics.append("FID")

        # SRA
        self.add_state("SRA", default=torch.tensor(0.0), dist_reduce_fx="sum")
        self.metrics.append("SRA")

        self.add_state("SRA_3", default=torch.tensor(0.0), dist_reduce_fx="sum")
        self.metrics.append("SRA_3")

        self.add_state("SRA_5", default=torch.tensor(0.0), dist_reduce_fx="sum")
        self.metrics.append("SRA_5")

        # Diversity
        self.add_state("Diversity",
                       default=torch.tensor(0.0),
                       dist_reduce_fx="sum")
        self.add_state("gt_Diversity",
                       default=torch.tensor(0.0),
                       dist_reduce_fx="sum")
        self.metrics.extend(["Diversity", "gt_Diversity"])

        # chached batches
        # self.add_state("text_embeddings", default=[], dist_reduce_fx=None)
        self.add_state("recmotion_embeddings", default=[], dist_reduce_fx=None)
        self.add_state("gtmotion_embeddings", default=[], dist_reduce_fx=None)

        self.add_state("predicted", default=[], dist_reduce_fx=None)
        self.add_state("label", default=[], dist_reduce_fx=None)

    def compute(self, sanity_flag):
        count = self.count.item()
        count_seq = self.count_seq.item()

        # init metrics
        metrics = {metric: getattr(self, metric) for metric in self.metrics}

        # if in sanity check stage then jump
        if sanity_flag:
            return metrics

        # cat all embeddings
        shuffle_idx = torch.randperm(count_seq)
        # all_texts = torch.cat(self.text_embeddings,
        #                       axis=0).cpu()[shuffle_idx, :]
        all_genmotions = torch.cat(self.recmotion_embeddings,
                                   axis=0).cpu()[shuffle_idx, :]
        all_gtmotions = torch.cat(self.gtmotion_embeddings,
                                  axis=0).cpu()[shuffle_idx, :]

        all_predicted = torch.cat(self.predicted,
                              axis=0).cpu()[shuffle_idx, :]

        all_label = torch.cat(self.label,
                              axis=0).cpu()[shuffle_idx, :]

        # Compute r-precision
        self.R_size = 20
        assert count_seq > self.R_size
        top_k_mat = torch.zeros((self.top_k, ))

        # tensor -> numpy for FID
        all_genmotions = all_genmotions.numpy()
        all_gtmotions = all_gtmotions.numpy()

        # Compute fid
        mu, cov = calculate_activation_statistics_np(all_genmotions)
        # gt_mu, gt_cov = calculate_activation_statistics_np(all_gtmotions)
        gt_mu, gt_cov = calculate_activation_statistics_np(all_gtmotions)
        metrics["FID"] = calculate_frechet_distance_np(gt_mu, gt_cov, mu, cov)

        output = topk_accuracy(all_predicted,all_label)
        metrics["SRA_3"] = output[1]
        metrics["SRA_5"] = output[2]
        metrics["SRA"] = output[0]#calculate_SRA(all_predicted.numpy(), all_label.numpy())

        # metrics["SRA"] = calculate_SRA(all_predicted.numpy(), all_label.numpy())

        # Compute diversity
        assert count_seq > self.diversity_times
        metrics["Diversity"] = calculate_diversity_np(all_genmotions,
                                                      self.diversity_times)
        metrics["gt_Diversity"] = calculate_diversity_np(
            all_gtmotions, self.diversity_times)

        return {**metrics}

    def update(
        self,
        # text_embeddings: Tensor,
        recmotion_embeddings: Tensor,
        gtmotion_embeddings: Tensor,
        lengths: List[int],
        predicted: Tensor,
        label: Tensor
    ):
        self.count += sum(lengths)
        self.count_seq += len(lengths)

        # [bs, nlatent*ndim] <= [bs, nlatent, ndim]
        # text_embeddings = torch.flatten(text_embeddings, start_dim=1).detach()
        recmotion_embeddings = torch.flatten(recmotion_embeddings,
                                             start_dim=1).detach()
        gtmotion_embeddings = torch.flatten(gtmotion_embeddings,
                                            start_dim=1).detach()



        predicted = predicted.unsqueeze(0).unsqueeze(0)
        label = label.unsqueeze(0).unsqueeze(0)


        # store all texts and motions
        # self.text_embeddings.append(text_embeddings)
        self.recmotion_embeddings.append(recmotion_embeddings)
        self.gtmotion_embeddings.append(gtmotion_embeddings)
        self.predicted.append(predicted)
        self.label.append(label)



class TM2TMetrics_MST(Metric):
    full_state_update = True

    def __init__(self,
                 top_k=3,
                 R_size=32,
                 diversity_times=3,
                 dist_sync_on_step=True,
                 **kwargs):
        super().__init__(dist_sync_on_step=dist_sync_on_step)

        self.name = "matching, fid, and diversity scores"

        self.top_k = top_k
        self.R_size = R_size
        self.diversity_times = 80#diversity_times

        self.add_state("count", default=torch.tensor(0), dist_reduce_fx="sum")
        self.add_state("count_seq",
                       default=torch.tensor(0),
                       dist_reduce_fx="sum")

        self.metrics = []

        # Fid
        self.add_state("FID", default=torch.tensor(0.0), dist_reduce_fx="sum")
        self.metrics.append("FID")

        # SRA
        self.add_state("SRA", default=torch.tensor(0.0), dist_reduce_fx="sum")
        self.metrics.append("SRA")

        self.add_state("SRA_3", default=torch.tensor(0.0), dist_reduce_fx="sum")
        self.metrics.append("SRA_3")

        self.add_state("SRA_5", default=torch.tensor(0.0), dist_reduce_fx="sum")
        self.metrics.append("SRA_5")

        # # Diversity
        # self.add_state("Diversity",
        #                default=torch.tensor(0.0),
        #                dist_reduce_fx="sum")
        # self.add_state("gt_Diversity",
        #                default=torch.tensor(0.0),
        #                dist_reduce_fx="sum")
        # self.metrics.extend(["Diversity", "gt_Diversity"])

        # chached batches
        # self.add_state("text_embeddings", default=[], dist_reduce_fx=None)
        self.add_state("recmotion_embeddings", default=[], dist_reduce_fx=None)
        self.add_state("gtmotion_embeddings", default=[], dist_reduce_fx=None)

        self.add_state("predicted", default=[], dist_reduce_fx=None)
        self.add_state("label", default=[], dist_reduce_fx=None)

        self.add_state("skate_ratio", default=torch.tensor(0.0), dist_reduce_fx="sum")
        self.metrics.append("skate_ratio")

        self.add_state("joints_rst", default=[], dist_reduce_fx=None)

    def compute(self, sanity_flag):
        count = self.count.item()
        count_seq = self.count_seq.item()

        # init metrics
        metrics = {metric: getattr(self, metric) for metric in self.metrics}

        # if in sanity check stage then jump
        if sanity_flag:
            return metrics

        # cat all embeddings
        shuffle_idx = torch.randperm(count_seq)
        # all_texts = torch.cat(self.text_embeddings,
        #                       axis=0).cpu()[shuffle_idx, :]
        all_genmotions = torch.cat(self.recmotion_embeddings,
                                   axis=0).cpu()[shuffle_idx, :]
        all_gtmotions = torch.cat(self.gtmotion_embeddings,
                                  axis=0).cpu()[shuffle_idx, :]

        all_predicted = torch.cat(self.predicted,
                              axis=0).cpu()[shuffle_idx, :]

        all_label = torch.cat(self.label,
                              axis=0).cpu()[shuffle_idx, :]

        # Compute r-precision
        # self.R_size = 20
        # assert count_seq > self.R_size
        # top_k_mat = torch.zeros((self.top_k, ))

        # tensor -> numpy for FID
        all_genmotions = all_genmotions.numpy()
        all_gtmotions = all_gtmotions.numpy()

        all_joints_rst = self.joints_rst

        # Compute fid
        mu, cov = calculate_activation_statistics_np(all_genmotions)
        # gt_mu, gt_cov = calculate_activation_statistics_np(all_gtmotions)
        gt_mu, gt_cov = calculate_activation_statistics_np(all_gtmotions)
        metrics["FID"] = calculate_frechet_distance_np(gt_mu, gt_cov, mu, cov)

        # Compute skate_ratio
        # motions [bs, 22, 3, max_len]
        skate_ratio_sum = 0.0
        for index in range(0,len(all_joints_rst)):
            # joints = all_joints_rst[index]
            skate_ratio, skate_vel = calculate_skating_ratio(all_joints_rst[index].permute(0, 2, 3, 1))
            skate_ratio_sum += skate_ratio

        # skate_ratio, skate_vel = calculate_skating_ratio(all_joints_rst.permute(0, 2, 3, 1))
        metrics["skate_ratio"] = skate_ratio_sum / len(all_joints_rst)


        output = topk_accuracy(all_predicted,all_label)
        metrics["SRA_3"] = output[1]
        metrics["SRA_5"] = output[2]
        metrics["SRA"] = output[0]#calculate_SRA(all_predicted.numpy(), all_label.numpy())

        # metrics["SRA"] = calculate_SRA(all_predicted.numpy(), all_label.numpy())

        # Compute diversity
        # assert count_seq > self.diversity_times
        # metrics["Diversity"] = calculate_diversity_np(all_genmotions,
        #                                               self.diversity_times)
        # metrics["gt_Diversity"] = calculate_diversity_np(
        #     all_gtmotions, self.diversity_times)

        return {**metrics}

    def update(
        self,
        # text_embeddings: Tensor,
        recmotion_embeddings: Tensor,
        gtmotion_embeddings: Tensor,
        lengths: List[int],
        predicted: Tensor,
        label: Tensor,

        joints_rst: Tensor,
    ):
        self.count += sum(lengths)
        self.count_seq += len(lengths)

        # [bs, nlatent*ndim] <= [bs, nlatent, ndim]
        # text_embeddings = torch.flatten(text_embeddings, start_dim=1).detach()
        recmotion_embeddings = torch.flatten(recmotion_embeddings,
                                             start_dim=1).detach()
        gtmotion_embeddings = torch.flatten(gtmotion_embeddings,
                                            start_dim=1).detach()


        predicted = predicted.unsqueeze(0).unsqueeze(0)
        label = label.unsqueeze(0).unsqueeze(0)


        # store all texts and motions
        # self.text_embeddings.append(text_embeddings)
        self.recmotion_embeddings.append(recmotion_embeddings)
        self.gtmotion_embeddings.append(gtmotion_embeddings)
        self.predicted.append(predicted)
        self.label.append(label)

        self.joints_rst.append(joints_rst)


class TM2TMetrics_MST_XIA(Metric):
    full_state_update = True

    def __init__(self,
                 top_k=3,
                 R_size=32,
                 diversity_times=3,
                 dist_sync_on_step=True,
                 **kwargs):
        super().__init__(dist_sync_on_step=dist_sync_on_step)

        self.name = "matching, fid, and diversity scores"

        self.top_k = top_k
        self.R_size = R_size
        self.diversity_times = 80#diversity_times

        self.add_state("count", default=torch.tensor(0), dist_reduce_fx="sum")
        self.add_state("count_seq",
                       default=torch.tensor(0),
                       dist_reduce_fx="sum")

        self.metrics = []

        # Fid
        self.add_state("FID", default=torch.tensor(0.0), dist_reduce_fx="sum")
        self.metrics.append("FID")

        # SRA
        self.add_state("SRA", default=torch.tensor(0.0), dist_reduce_fx="sum")
        self.metrics.append("SRA")

        self.add_state("SRA_3", default=torch.tensor(0.0), dist_reduce_fx="sum")
        self.metrics.append("SRA_3")

        self.add_state("SRA_5", default=torch.tensor(0.0), dist_reduce_fx="sum")
        self.metrics.append("SRA_5")


        self.add_state("CRA", default=torch.tensor(0.0), dist_reduce_fx="sum")
        self.metrics.append("CRA")
        # # Diversity

        # chached batches
        # self.add_state("text_embeddings", default=[], dist_reduce_fx=None)
        self.add_state("recmotion_embeddings", default=[], dist_reduce_fx=None)
        self.add_state("gtmotion_embeddings", default=[], dist_reduce_fx=None)

        self.add_state("predicted", default=[], dist_reduce_fx=None)
        self.add_state("label", default=[], dist_reduce_fx=None)

        self.add_state("predicted_c", default=[], dist_reduce_fx=None)
        self.add_state("label_c", default=[], dist_reduce_fx=None)

        self.add_state("skate_ratio", default=torch.tensor(0.0), dist_reduce_fx="sum")
        self.metrics.append("skate_ratio")

        self.add_state("joints_rst", default=[], dist_reduce_fx=None)

    def compute(self, sanity_flag):
        count = self.count.item()
        count_seq = self.count_seq.item()

        # init metrics
        metrics = {metric: getattr(self, metric) for metric in self.metrics}

        # if in sanity check stage then jump
        if sanity_flag:
            return metrics

        # cat all embeddings
        shuffle_idx = torch.randperm(count_seq)
        # all_texts = torch.cat(self.text_embeddings,
        #                       axis=0).cpu()[shuffle_idx, :]
        all_genmotions = torch.cat(self.recmotion_embeddings,
                                   axis=0).cpu()[shuffle_idx, :]
        all_gtmotions = torch.cat(self.gtmotion_embeddings,
                                  axis=0).cpu()[shuffle_idx, :]

        all_predicted = torch.cat(self.predicted,
                              axis=0).cpu()[shuffle_idx, :]

        all_label = torch.cat(self.label,
                              axis=0).cpu()[shuffle_idx, :]


        all_predicted_c = torch.cat(self.predicted_c,
                              axis=0).cpu()[shuffle_idx, :]

        all_label_c = torch.cat(self.label_c,
                              axis=0).cpu()[shuffle_idx, :]

        # tensor -> numpy for FID
        all_genmotions = all_genmotions.numpy()
        all_gtmotions = all_gtmotions.numpy()

        all_joints_rst = self.joints_rst

        # Compute fid
        mu, cov = calculate_activation_statistics_np(all_genmotions)
        # gt_mu, gt_cov = calculate_activation_statistics_np(all_gtmotions)
        gt_mu, gt_cov = calculate_activation_statistics_np(all_gtmotions)
        metrics["FID"] = calculate_frechet_distance_np(gt_mu, gt_cov, mu, cov)

        # Compute skate_ratio
        # motions [bs, 22, 3, max_len]
        skate_ratio_sum = 0.0
        for index in range(0,len(all_joints_rst)):
            # joints = all_joints_rst[index]
            skate_ratio, skate_vel = calculate_skating_ratio(all_joints_rst[index].permute(0, 2, 3, 1))
            skate_ratio_sum += skate_ratio

        # skate_ratio, skate_vel = calculate_skating_ratio(all_joints_rst.permute(0, 2, 3, 1))
        metrics["skate_ratio"] = skate_ratio_sum / len(all_joints_rst)


        output = topk_accuracy(all_predicted,all_label)
        metrics["SRA_3"] = output[1]
        metrics["SRA_5"] = output[2]
        metrics["SRA"] = output[0]

        output_c = topk_accuracy(all_predicted_c,all_label_c)
        metrics["CRA"] = output_c[0]


        return {**metrics}

    def update(
        self,
        # text_embeddings: Tensor,
        recmotion_embeddings: Tensor,
        gtmotion_embeddings: Tensor,
        lengths: List[int],
        predicted: Tensor,
        label: Tensor,

        predicted_c: Tensor,
        label_c: Tensor,

        joints_rst: Tensor,
    ):
        self.count += sum(lengths)
        self.count_seq += len(lengths)

        # [bs, nlatent*ndim] <= [bs, nlatent, ndim]
        recmotion_embeddings = torch.flatten(recmotion_embeddings,
                                             start_dim=1).detach()
        gtmotion_embeddings = torch.flatten(gtmotion_embeddings,
                                            start_dim=1).detach()


        predicted = predicted.unsqueeze(0).unsqueeze(0)
        label = label.unsqueeze(0).unsqueeze(0)

        predicted_c = predicted_c.unsqueeze(0).unsqueeze(0)
        label_c = label_c.unsqueeze(0).unsqueeze(0)


        # store all texts and motions
        self.recmotion_embeddings.append(recmotion_embeddings)
        self.gtmotion_embeddings.append(gtmotion_embeddings)
        self.predicted.append(predicted)
        self.label.append(label)

        self.predicted_c.append(predicted_c)
        self.label_c.append(label_c)

        self.joints_rst.append(joints_rst)
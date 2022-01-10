# -*- coding: utf-8 -*-
import inspect
import os
import time
from os import path as osp

import psds_eval
import scipy
from dcase_util.data import ProbabilityEncoder
from data_utils.DataLoad import data_prefetcher
import sed_eval
import numpy as np
import pandas as pd
import torch
from psds_eval import plot_psd_roc, PSDSEval
from utilities.utils import MetricLogger
import config as cfg
from utilities.Logger import create_logger
from utilities.FrameEncoder import ManyHotEncoder
from collections.abc import Iterable

logger = create_logger(__name__, terminal_level=cfg.terminal_level)

def flatten(l):
    for el in l:
        if isinstance(el, Iterable) and not isinstance(el, (str, bytes)):
            yield from flatten(el)
        else:
            yield el



def get_event_list_current_file(df, fname):
    """
    Get list of events for a given filename
    :param df: pd.DataFrame, the dataframe to search on
    :param fname: the filename to extract the value from the dataframe
    :return: list of events (dictionaries) for the given filename
    """
    event_file = df[df["filename"] == fname]
    if len(event_file) == 1:
        if pd.isna(event_file["event_label"].iloc[0]):
            event_list_for_current_file = [{"filename": fname}]
        else:
            event_list_for_current_file = event_file.to_dict('records')
    else:
        event_list_for_current_file = event_file.to_dict('records')

    return event_list_for_current_file


def event_based_evaluation_df(reference, estimated, t_collar=0.200, percentage_of_length=0.2):
    """ Calculate EventBasedMetric given a reference and estimated dataframe

    Args:
        reference: pd.DataFrame containing "filename" "onset" "offset" and "event_label" columns which describe the
            reference events
        estimated: pd.DataFrame containing "filename" "onset" "offset" and "event_label" columns which describe the
            estimated events to be compared with reference
        t_collar: float, in seconds, the number of time allowed on onsets and offsets
        percentage_of_length: float, between 0 and 1, the percentage of length of the file allowed on the offset
    Returns:
         sed_eval.sound_event.EventBasedMetrics with the scores
    """

    evaluated_files = reference["filename"].unique()

    classes = []
    classes.extend(reference.event_label.dropna().unique())
    classes.extend(estimated.event_label.dropna().unique())
    classes = list(set(classes))

    event_based_metric = sed_eval.sound_event.EventBasedMetrics(
        event_label_list=classes,
        t_collar=t_collar,
        percentage_of_length=percentage_of_length,
        empty_system_output_handling='zero_score'
    )

    for fname in evaluated_files:
        reference_event_list_for_current_file = get_event_list_current_file(reference, fname)
        estimated_event_list_for_current_file = get_event_list_current_file(estimated, fname)

        event_based_metric.evaluate(
            reference_event_list=reference_event_list_for_current_file,
            estimated_event_list=estimated_event_list_for_current_file,
        )

    return event_based_metric


def segment_based_evaluation_df(reference, estimated, time_resolution=1.):
    """ Calculate SegmentBasedMetrics given a reference and estimated dataframe

        Args:
            reference: pd.DataFrame containing "filename" "onset" "offset" and "event_label" columns which describe the
                reference events
            estimated: pd.DataFrame containing "filename" "onset" "offset" and "event_label" columns which describe the
                estimated events to be compared with reference
            time_resolution: float, the time resolution of the segment based metric
        Returns:
             sed_eval.sound_event.SegmentBasedMetrics with the scores
        """
    evaluated_files = reference["filename"].unique()

    classes = []
    classes.extend(reference.event_label.dropna().unique())
    classes.extend(estimated.event_label.dropna().unique())
    classes = list(set(classes))

    segment_based_metric = sed_eval.sound_event.SegmentBasedMetrics(
        event_label_list=classes,
        time_resolution=time_resolution
    )

    for fname in evaluated_files:
        reference_event_list_for_current_file = get_event_list_current_file(reference, fname)
        estimated_event_list_for_current_file = get_event_list_current_file(estimated, fname)

        segment_based_metric.evaluate(
            reference_event_list=reference_event_list_for_current_file,
            estimated_event_list=estimated_event_list_for_current_file
        )

    return segment_based_metric





def get_sedt_predictions(model, criterion, postprocessors, dataloader, decoder, fusion_strategy=[1], at=True):
    """ Get the predictions of a trained model on a specific set
    Args:
        model: torch.Module, a trained pytorch model (you usually want it to be in .eval() mode).
        dataloader: torch.utils.data.DataLoader, giving ((input_data, label), indexes) but label is not used here
        decoder: function, takes a numpy.array of shape (time_steps, n_labels) as input and return a list of lists
            of ("event_label", "onset", "offset") for each label predicted.

    Returns:
        dict of the different predictions with associated threshold
    """
    logger = create_logger(__name__ + "/" + inspect.currentframe().f_code.co_name, terminal_level=cfg.terminal_level)
    # Init a dataframe per threshold
    metric_logger = MetricLogger(delimiter="  ")
    # metric_logger.add_meter('class_error', utils.SmoothedValue(window_size=1, fmt='{value:.2f}'))
    epoch_time = time.time()
    decoding_time = 0.
    dec_prediction_dfs = {}
    audio_tag_dfs = pd.DataFrame()
    for at_m in fusion_strategy:
        dec_prediction_dfs[at_m] = pd.DataFrame()

    # Get predictions
    prefetcher = data_prefetcher(dataloader, return_indexes=True)
    i = -1
    (input_data, targets), indexes = prefetcher.next()
    while input_data is not None:
        i += 1
        with torch.no_grad():
            outputs = model(input_data)
        # ##############
        # compute losses
        # ##############
        weak_mask = None  # slice(cfg.batch_size)
        strong_mask = slice(len(input_data.tensors))
        loss_dict, indices = criterion(outputs, targets, weak_mask, strong_mask)
        weight_dict = criterion.weight_dict

        # reduce losses over all GPUs for logging purposes
        loss_dict_unscaled = {f'{k}_unscaled': v for k, v in loss_dict.items()}
        loss_dict_scaled = {k: v * weight_dict[k] for k, v in loss_dict.items() if k in weight_dict}
        losses_scaled = sum(loss_dict_scaled.values())

        loss_value = losses_scaled.item()
        metric_logger.update(loss=loss_value, **loss_dict_scaled, **loss_dict_unscaled)


        # ###################
        # get decoder results
        # ###################
        orig_target_sizes = torch.stack([t["orig_size"] for t in targets], dim=0)
        if at:
            assert "at" in outputs
            audio_tags = outputs["at"]
            audio_tags = (audio_tags > 0.5).long()
            for j, audio_tag in enumerate(audio_tags):
                audio_tag_res = decoder.decode_weak(audio_tag)
                audio_tag_res = pd.DataFrame(audio_tag_res, columns=["event_label"])
                audio_tag_res["filename"] = dataloader.dataset.filenames.iloc[indexes[j]]
                audio_tag_res["onset"] = 0
                audio_tag_res["offset"] = 0
                audio_tag_dfs = audio_tag_dfs.append(audio_tag_res)
        else:
            audio_tags = None

        decoding_start = time.time()
        for at_m in fusion_strategy:
            results = postprocessors['bbox'](outputs, orig_target_sizes, audio_tags=audio_tags, at_m=at_m)

            for j, res in enumerate(results):
                for item in res:
                    res[item] = res[item].cpu()
                pred = decoder.decode_strong(res, threshold=0.5)
                pred = pd.DataFrame(pred, columns=["event_label", "onset", "offset", "score"])
                # Put them in seconds
                pred.loc[:, ["onset", "offset"]] = pred[["onset", "offset"]].clip(0, cfg.max_len_seconds)
                pred["filename"] = dataloader.dataset.filenames.iloc[indexes[j]]
                dec_prediction_dfs[at_m] = dec_prediction_dfs[at_m].append(pred, ignore_index=True)

        decoding_time += time.time() - decoding_start
        (input_data, targets), indexes = prefetcher.next()


    logger.info("Val averaged stats:" + metric_logger.__str__())

    epoch_time = time.time() - epoch_time
    logger.info(f"val_epoch_time:{epoch_time}  decoding_time:{decoding_time}")
    return audio_tag_dfs, dec_prediction_dfs




def psds_score(psds, filename_roc_curves=None):
    """ add operating points to PSDSEval object and compute metrics

    Args:
        psds: psds.PSDSEval object initialized with the groundtruth corresponding to the predictions
        filename_roc_curves: str, the base filename of the roc curve to be computed
    """
    try:
        psds_score = psds.psds(alpha_ct=0, alpha_st=0, max_efpr=100)
        logger.info(f"\nPSD-Score (0, 0, 100): {psds_score.value:.5f}")
        psds_ct_score = psds.psds(alpha_ct=1, alpha_st=0, max_efpr=100)
        logger.info(f"\nPSD-Score (1, 0, 100): {psds_ct_score.value:.5f}")
        psds_macro_score = psds.psds(alpha_ct=0, alpha_st=1, max_efpr=100)
        logger.info(f"\nPSD-Score (0, 1, 100): {psds_macro_score.value:.5f}")
        if filename_roc_curves is not None:
            if osp.dirname(filename_roc_curves) != "":
                os.makedirs(osp.dirname(filename_roc_curves), exist_ok=True)
            base, ext = osp.splitext(filename_roc_curves)
            plot_psd_roc(psds_score, filename=f"{base}_0_0_100{ext}")
            plot_psd_roc(psds_ct_score, filename=f"{base}_1_0_100{ext}")
            plot_psd_roc(psds_macro_score, filename=f"{base}_0_1_100{ext}")

    except psds_eval.psds.PSDSEvalError as e:
        logger.error("psds score did not work ....")
        logger.error(e)


def compute_sed_eval_metrics(predictions, groundtruth, report=True, cal_seg=False):
    metric_event = event_based_evaluation_df(groundtruth, predictions, t_collar=0.200,
                                             percentage_of_length=0.2)
    if report:
        # logger.info(metric_event)
        print(metric_event)
    metric_segment = None
    if cal_seg:
        metric_segment = segment_based_evaluation_df(groundtruth, predictions, time_resolution=1.)
        # logger.info(metric_segment)
        print(metric_segment)
    return metric_event, metric_segment


def format_df(df, mhe):
    """ Make a weak labels dataframe from strongly labeled (join labels)
    Args:
        df: pd.DataFrame, the dataframe strongly labeled with onset and offset columns (+ event_label)
        mhe: ManyHotEncoder object, the many hot encoder object that can encode the weak labels

    Returns:
        weakly labeled dataframe
    """

    def join_labels(x):
        return pd.Series(dict(filename=x['filename'].iloc[0],
                              event_label=mhe.encode_weak(x["event_label"].drop_duplicates().dropna().tolist())))

    if "onset" in df.columns or "offset" in df.columns:
        df = df.groupby("filename", as_index=False).apply(join_labels)
    return df


def get_f_measure_by_class(torch_model, nb_tags, dataloader_, thresholds_=None):
    """ get f measure for each class given a model and a generator of data (batch_x, y)

    Args:
        torch_model : Model, model to get predictions, forward should return weak and strong predictions
        nb_tags : int, number of classes which are represented
        dataloader_ : generator, data generator used to get f_measure
        thresholds_ : int or list, thresholds to apply to each class to binarize probabilities

    Returns:
        macro_f_measure : list, f measure for each class

    """
    if torch.cuda.is_available():
        torch_model = torch_model.cuda()

    # Calculate external metrics
    tp = np.zeros(nb_tags)
    tn = np.zeros(nb_tags)
    fp = np.zeros(nb_tags)
    fn = np.zeros(nb_tags)
    for counter, (batch_x, y) in enumerate(dataloader_):
        if torch.cuda.is_available():
            batch_x = batch_x.cuda()

        pred_strong, pred_weak = torch_model(batch_x)
        pred_weak = pred_weak.cpu().data.numpy()
        labels = y.numpy()

        # Used only with a model predicting only strong outputs
        if len(pred_weak.shape) == 3:
            # average data to have weak labels
            pred_weak = np.max(pred_weak, axis=1)

        if len(labels.shape) == 3:
            labels = np.max(labels, axis=1)
            labels = ProbabilityEncoder().binarization(labels,
                                                       binarization_type="global_threshold",
                                                       threshold=0.5)

        if thresholds_ is None:
            binarization_type = 'global_threshold'
            thresh = 0.5
        else:
            binarization_type = "class_threshold"
            assert type(thresholds_) is list
            thresh = thresholds_

        batch_predictions = ProbabilityEncoder().binarization(pred_weak,
                                                              binarization_type=binarization_type,
                                                              threshold=thresh,
                                                              time_axis=0
                                                              )

        tp_, fp_, fn_, tn_ = intermediate_at_measures(labels, batch_predictions)
        tp += tp_
        fp += fp_
        fn += fn_
        tn += tn_

    macro_f_score = np.zeros(nb_tags)
    mask_f_score = 2 * tp + fp + fn != 0
    macro_f_score[mask_f_score] = 2 * tp[mask_f_score] / (2 * tp + fp + fn)[mask_f_score]

    return macro_f_score


def intermediate_at_measures(encoded_ref, encoded_est):
    """ Calculate true/false - positives/negatives.

    Args:
        encoded_ref: np.array, the reference array where a 1 means the label is present, 0 otherwise
        encoded_est: np.array, the estimated array, where a 1 means the label is present, 0 otherwise

    Returns:
        tuple
        number of (true positives, false positives, false negatives, true negatives)

    """
    tp = (encoded_est + encoded_ref == 2).sum(axis=0)
    fp = (encoded_est - encoded_ref == 1).sum(axis=0)
    fn = (encoded_ref - encoded_est == 1).sum(axis=0)
    tn = (encoded_est + encoded_ref == 0).sum(axis=0)
    return tp, fp, fn, tn


def macro_f_measure(tp, fp, fn):
    """ From intermediates measures, give the macro F-measure

    Args:
        tp: int, number of true positives
        fp: int, number of false positives
        fn: int, number of true negatives

    Returns:
        float
        The macro F-measure
    """
    macro_f_score = np.zeros(tp.shape[-1])
    mask_f_score = 2 * tp + fp + fn != 0
    macro_f_score[mask_f_score] = 2 * tp[mask_f_score] / (2 * tp + fp + fn)[mask_f_score]
    return macro_f_score


def audio_tagging_results(reference, estimated):
    classes = []
    if "event_label" in reference.columns:
        classes.extend(reference.event_label.dropna().unique())
        classes.extend(estimated.event_label.dropna().unique())
        classes = list(set(classes))
        mhe = ManyHotEncoder(classes)
        reference = format_df(reference, mhe)
        estimated = format_df(estimated, mhe)
    else:
        classes.extend(reference.event_labels.str.split(',', expand=True).unstack().dropna().unique())
        classes.extend(estimated.event_labels.str.split(',', expand=True).unstack().dropna().unique())
        classes = list(set(classes))
        mhe = ManyHotEncoder(classes)

    matching = reference.merge(estimated, how='outer', on="filename", suffixes=["_ref", "_pred"])

    def na_values(val):
        if type(val) is np.ndarray:
            return val
        if pd.isna(val):
            return np.zeros(len(classes))
        return val

    if not estimated.empty:
        matching.event_label_pred = matching.event_label_pred.apply(na_values)
        matching.event_label_ref = matching.event_label_ref.apply(na_values)

        tp, fp, fn, tn = intermediate_at_measures(np.array(matching.event_label_ref.tolist()),
                                                  np.array(matching.event_label_pred.tolist()))
        macro_f = macro_f_measure(tp, fp, fn)
        macro_p = tp / (tp + fp)
        macro_r = tp / (tp + fn)
    else:
        macro_f = np.zeros(len(classes))
        macro_p = np.zeros(len(classes))
        macro_r = np.zeros(len(classes))
    data = np.asarray([macro_f, macro_p, macro_r]).transpose(1, 0)
    results_serie = pd.DataFrame(data, columns=['f', 'p', 'r'], index=mhe.labels)
    results_serie = results_serie.append(
        pd.DataFrame(data.mean(0).reshape(1, -1), columns=['f', 'p', 'r'], index=['avg']))
    # return results_serie[0]
    return results_serie


def compute_psds_from_operating_points(list_predictions, groundtruth_df, meta_df, dtc_threshold=0.5, gtc_threshold=0.5,
                                       cttc_threshold=0.3):
    psds = PSDSEval(dtc_threshold, gtc_threshold, cttc_threshold, ground_truth=groundtruth_df, metadata=meta_df)
    for prediction_df in list_predictions:
        psds.add_operating_point(prediction_df)
    return psds


def compute_metrics(predictions, gtruth_df, meta_df=None, cal_seg=True, cal_clip=True):
    # report results
    if predictions.empty:
        return 0, 0, 0
    events_metric, segments_metric = compute_sed_eval_metrics(predictions, gtruth_df, report=True, cal_seg=cal_seg)
    events_macro = events_metric.results_class_wise_average_metrics()
    events_macro_f1 = events_macro['f_measure']['f_measure']
    events_macro_p = events_macro['f_measure']['precision']
    events_macro_r = events_macro['f_measure']['recall']
    clip_macro_f1 = None
    if cal_clip:
        clip_metric = audio_tagging_results(gtruth_df, predictions)
        # clip_macro_f1 = clip_metric.values.mean()
        clip_macro_f1 = clip_metric.loc['avg', 'f']
        print("Class-wise clip metrics")
        print("=" * 50)
        print(clip_metric)
    if segments_metric is not None:
        seg_macro = segments_metric.results_class_wise_average_metrics()
        seg_macro_f1 = seg_macro['f_measure']['f_measure']
        seg_macro_p = seg_macro['f_measure']['precision']
        seg_macro_r = seg_macro['f_measure']['recall']
        metric = pd.DataFrame([[str(events_macro_f1 * 100)[:5] + '%', str(events_macro_p * 100)[:5] + '%',
                                str(events_macro_r * 100)[:5] + '%', str(seg_macro_f1 * 100)[:5] + '%',
                                str(seg_macro_p * 100)[:5] + '%', str(seg_macro_r * 100)[:5] + '%',
                                str(clip_macro_f1 * 100)[:5] + '%']],
                              columns=['eve_F', 'eve_P', 'eve_R', 'seg_F', 'seg_P', 'seg_R', 'clip_F'])
        print(metric)
    # dtc_threshold, gtc_threshold, cttc_threshold = 0.5, 0.5, 0.3
    # psds = PSDSEval(dtc_threshold, gtc_threshold, cttc_threshold, ground_truth=gtruth_df, metadata=meta_df)
    # psds_macro_f1, psds_f1_classes = psds.compute_macro_f_score(predictions)
    # logger.info(f"F1_score (psds_eval) accounting cross triggers: {psds_macro_f1}")
    return events_macro_f1, events_macro_p, clip_macro_f1  # , psds_macro_f1

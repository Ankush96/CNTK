# Copyright (c) Microsoft. All rights reserved.

# Licensed under the MIT license. See LICENSE.md file in the project root
# for full license information.
# ==============================================================================

from __future__ import print_function
import numpy as np
import os, sys
from matplotlib.pyplot import imsave
from PIL import ImageFont
import cv2
import cntk
from cntk import Trainer, UnitType, load_model, user_function, Axis, input, parameter, times, combine, relu, \
    softmax, roipooling, reduce_sum, slice, splice, reshape, plus, CloneMethod, minus, element_times, alias
from cntk.io import MinibatchSource, ImageDeserializer, CTFDeserializer, StreamDefs, StreamDef, TraceLevel
from cntk.io.transforms import scale
from cntk.initializer import glorot_uniform
from cntk.layers import placeholder, Convolution, Constant, Sequential
from cntk.learners import momentum_sgd, learning_rate_schedule, momentum_schedule
from cntk.logging import log_number_of_parameters, ProgressPrinter
from cntk.logging.graph import find_by_name, plot
from cntk.losses import cross_entropy_with_softmax
from cntk.metrics import classification_error
from lib.rpn.anchor_target_layer import AnchorTargetLayer
from lib.rpn.proposal_layer import ProposalLayer
from lib.rpn.proposal_target_layer import ProposalTargetLayer
from lib.rpn.cntk_smoothL1_loss import SmoothL1Loss
from lib.rpn.cntk_ignore_label import IgnoreLabel
from cntk_helpers import visualizeResultsFaster
from lib.fast_rcnn.config import cfg
from lib.fast_rcnn.bbox_transform import bbox_transform_inv

available_font = "arial.ttf"
try:
    dummy = ImageFont.truetype(available_font, 16)
except:
    available_font = "FreeMono.ttf"

abs_path = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(abs_path, "..", ".."))

###############################################################
###############################################################
train_e2e = True
make_mode = False
graph_type = "png" # "png" or "pdf"
DEBUG_OUTPUT = cfg["CNTK"].DEBUG_OUTPUT
reader_trace_level = TraceLevel.Error

# stream names and paths
features_stream_name = 'features'
roi_stream_name = 'roiAndLabel'
output_path = os.path.join(abs_path, "Output")

num_input_rois = cfg["CNTK"].INPUT_ROIS_PER_IMAGE
num_channels = 3
image_height = 1000
image_width = 1000
mb_size = 1
max_epochs = cfg["CNTK"].MAX_EPOCHS
im_info = [image_width, image_height, 1]

# dataset specific parameters
dataset = cfg["CNTK"].DATASET
if dataset == "Grocery":
    classes = ('__background__',  # always index 0
               'avocado', 'orange', 'butter', 'champagne', 'eggBox', 'gerkin', 'joghurt', 'ketchup',
               'orangeJuice', 'onion', 'pepper', 'tomato', 'water', 'milk', 'tabasco', 'mustard')
    base_path = os.path.join(abs_path, "Data", "Grocery")
    train_map_file = os.path.join(base_path, "train.imgMap.txt")
    test_map_file = os.path.join(base_path, "test.imgMap.txt")
    train_roi_file = os.path.join(base_path, "train.GTRois.txt")
    test_roi_file = os.path.join(base_path, "test.GTRois.txt")
    num_classes = len(classes)
    epoch_size = 20
    num_test_images = 5
elif dataset == "Pascal":
    classes = ('__background__',  # always index 0
               'aeroplane', 'bicycle', 'bird', 'boat', 'bottle', 'bus', 'car', 'cat', 'chair', 'cow', 'diningtable',
               'dog', 'horse', 'motorbike', 'person', 'pottedplant', 'sheep', 'sofa', 'train', 'tvmonitor')
    base_path = os.path.join(abs_path, "Data", "Pascal")
    train_map_file = os.path.join(base_path, "trainval2007.txt")
    test_map_file = os.path.join(base_path, "test2007.txt")
    train_roi_file = os.path.join(base_path, "trainval2007_rois_topleft_wh_rel.txt")
    test_roi_file = os.path.join(base_path, "test2007_rois_topleft_wh_rel.txt")
    num_classes = len(classes)
    epoch_size = 5010
    num_test_images = 4952
else:
    raise ValueError('unknown data set: %s' % dataset)

# model specific variables
base_model_to_use = cfg["CNTK"].BASE_MODEL
model_folder = os.path.join(abs_path, "..", "..", "PretrainedModels")
if base_model_to_use == "AlexNet":
    base_model_file = os.path.join(model_folder, "AlexNet.model")
    feature_node_name = "features"
    last_conv_node_name = "conv5.y"
    start_train_conv_node_name = None # "conv3.y"
    pool_node_name = "pool3"
    last_hidden_node_name = "h2_d"
    roi_dim = 6
elif base_model_to_use == "VGG16":
    base_model_file = os.path.join(model_folder, "VGG16_ImageNet.cntkmodel")
    feature_node_name = "data"
    last_conv_node_name = "conv5_3"
    start_train_conv_node_name = None # "conv3_1"
    pool_node_name = "pool5"
    last_hidden_node_name = "drop7"
    roi_dim = 7
else:
    raise ValueError('unknown base model: %s' % base_model_to_use)
###############################################################
###############################################################


# Instantiates a composite minibatch source for reading images, roi coordinates and roi labels for training Fast R-CNN
def create_mb_source(img_map_file, roi_map_file, img_height, img_width, img_channels, n_rois, randomize=True):
    rois_dim = 5 * n_rois

    if not os.path.exists(img_map_file) or not os.path.exists(roi_map_file):
        raise RuntimeError("File '%s' or '%s' does not exist. "
                           "Please run install_fastrcnn.py from Examples/Image/Detection/FastRCNN to fetch them" %
                           (img_map_file, roi_map_file))

    # read images
    transforms = [scale(width=img_width, height=img_height, channels=img_channels,
                        scale_mode="pad", pad_value=114, interpolations='linear')]

    image_source = ImageDeserializer(img_map_file, StreamDefs(
        features = StreamDef(field='image', transforms=transforms)))

    # read rois and labels
    roi_source = CTFDeserializer(roi_map_file, StreamDefs(
        roiAndLabel = StreamDef(field=roi_stream_name, shape=rois_dim, is_sparse=False)))

    # define a composite reader
    return MinibatchSource([image_source, roi_source], epoch_size=sys.maxsize,
                           randomize=randomize, trace_level=reader_trace_level)

def clone_model(base_model, from_node_names, to_node_names, clone_method):
    from_nodes = [find_by_name(base_model, node_name) for node_name in from_node_names]
    if None in from_nodes:
        print("Error: could not find all specified 'from_nodes' in clone. Looking for {}, found {}"
              .format(from_node_names, from_nodes))
    to_nodes = [find_by_name(base_model, node_name) for node_name in to_node_names]
    if None in to_nodes:
        print("Error: could not find all specified 'to_nodes' in clone. Looking for {}, found {}"
              .format(to_node_names, to_nodes))
    input_placeholders = dict(zip(from_nodes, [placeholder() for x in from_nodes]))
    cloned_net = combine(to_nodes).clone(clone_method, input_placeholders)
    return cloned_net

def create_rpn(conv_out, gt_boxes, train=True):
    # RPN network
    rpn_conv_3x3 = Convolution((3, 3), 256, activation=relu, pad=True, strides=1)(conv_out)
    rpn_cls_score = Convolution((1, 1), 18, activation=None, name="rpn_cls_score")(rpn_conv_3x3)  # 2(bg/fg)  * 9(anchors)
    rpn_bbox_pred = Convolution((1, 1), 36, activation=None, name="rpn_bbox_pred")(rpn_conv_3x3)  # 4(coords) * 9(anchors)

    # RPN targets
    # Comment: rpn_cls_score is only passed   vvv   to get width and height of the conv feature map ...
    atl = user_function(AnchorTargetLayer(rpn_cls_score, gt_boxes, im_info=im_info))
    rpn_labels = atl.outputs[0]
    rpn_bbox_targets = atl.outputs[1]
    rpn_bbox_inside_weights = atl.outputs[2]

    # getting rpn class scores and rpn targets into the correct shape for ce
    # i.e., (2, 33k), where each group of two corresponds to a (bg, fg) pair for score or target
    # Reshape scores
    num_anchors = int(rpn_cls_score.shape[0] / 2)
    num_predictions = int(np.prod(rpn_cls_score.shape) / 2)
    bg_scores = slice(rpn_cls_score, 0, 0, num_anchors)
    fg_scores = slice(rpn_cls_score, 0, num_anchors, num_anchors * 2)
    bg_scores_rshp = reshape(bg_scores, (1, num_predictions))
    fg_scores_rshp = reshape(fg_scores, (1, num_predictions))
    rpn_cls_score_rshp = splice(bg_scores_rshp, fg_scores_rshp, axis=0)
    rpn_cls_prob = softmax(rpn_cls_score_rshp, axis=0, name="objness_softmax")
    # Reshape targets
    rpn_labels_rshp = reshape(rpn_labels, (1, num_predictions))

    # Ignore label predictions for the 'ignore label', i.e. set target and prediction to 0 --> needs to be softmaxed before
    ignore = user_function(IgnoreLabel(rpn_cls_prob, rpn_labels_rshp, ignore_label=-1))
    rpn_cls_prob_ignore = ignore.outputs[0]
    fg_targets = ignore.outputs[1]
    bg_targets = 1 - fg_targets
    rpn_labels_ignore = splice(bg_targets, fg_targets, axis=0)

    # RPN losses
    rpn_loss_cls = cross_entropy_with_softmax(rpn_cls_prob_ignore, rpn_labels_ignore, axis=0)
    rpn_loss_bbox = user_function(SmoothL1Loss(rpn_bbox_pred, rpn_bbox_targets, rpn_bbox_inside_weights))
    rpn_losses = plus(reduce_sum(rpn_loss_cls), reduce_sum(rpn_loss_bbox), name="rpn_losses")

    # ROI proposal
    # - ProposalLayer:
    #    Outputs object detection proposals by applying estimated bounding-box
    #    transformations to a set of regular boxes (called "anchors").
    # - ProposalTargetLayer:
    #    Assign object detection proposals to ground-truth targets. Produces proposal
    #    classification labels and bounding-box regression targets.
    #  + adds gt_boxes to candidates and samples fg and bg rois for training

    # reshape predictions per (H, W) position to (2,9) ( == (bg, fg) per anchor)
    shp = (2, num_anchors,) + rpn_cls_score.shape[-2:]
    rpn_cls_prob_reshape = reshape(rpn_cls_prob, shp)

    rpn_rois_raw = user_function(ProposalLayer(rpn_cls_prob_reshape, rpn_bbox_pred, im_info=im_info))
    rpn_rois = alias(rpn_rois_raw, name='rpn_rois')

    return rpn_rois, rpn_losses

def create_fast_rcnn_predictor(conv_out, rois, fc_layers):
    # for the roipooling layer we convert and scale roi coords back to x, y, w, h relative from x1, y1, x2, y2 absolute
    roi_xy1 = slice(rois, 1, 0, 2)
    roi_xy2 = slice(rois, 1, 2, 4)
    roi_wh = minus(roi_xy2, roi_xy1)
    roi_xywh = splice(roi_xy1, roi_wh, axis=1)
    scaled_rois = element_times(roi_xywh, (1.0 / image_width))

    # RCNN
    roi_out = roipooling(conv_out, scaled_rois, (roi_dim, roi_dim))
    fc_out = fc_layers(roi_out)

    # prediction head
    W_pred = parameter(shape=(4096, num_classes), init=glorot_uniform())
    b_pred = parameter(shape=num_classes, init=0)
    cls_score = plus(times(fc_out, W_pred), b_pred, name='cls_score')

    # regression head
    W_regr = parameter(shape=(4096, num_classes*4), init=glorot_uniform())
    b_regr = parameter(shape=num_classes*4, init=0)
    bbox_pred = plus(times(fc_out, W_regr), b_regr, name='bbox_regr')

    return cls_score, bbox_pred

# Defines the Faster R-CNN network model for detecting objects in images
def faster_rcnn_predictor(features, gt_boxes):
    # Load the pre-trained classification net and clone layers
    base_model = load_model(base_model_file)
    conv_layers = clone_model(base_model, [feature_node_name], [last_conv_node_name], clone_method=CloneMethod.freeze)
    # TODO: reset to CloneMethod.clone. Setting to freeze for now to try learning rates
    fc_layers = clone_model(base_model, [pool_node_name], [last_hidden_node_name], clone_method=CloneMethod.freeze)

    # Normalization and conv layers
    feat_norm = features - Constant(114)
    conv_out = conv_layers(feat_norm)

    # RPN
    rpn_rois, rpn_losses = create_rpn(conv_out, gt_boxes)

    ptl = user_function(ProposalTargetLayer(rpn_rois, gt_boxes, num_classes=num_classes))
    rois = alias(ptl.outputs[0], name='rpn_target_rois')
    labels = alias(ptl.outputs[1], name='label_targets')
    bbox_targets = alias(ptl.outputs[2], name='bbox_targets')
    bbox_inside_weights = alias(ptl.outputs[3], name='bbox_inside_w')

    # Fast RCNN
    cls_score, bbox_pred = create_fast_rcnn_predictor(conv_out, rois, fc_layers)

    # loss functions
    loss_cls = cross_entropy_with_softmax(cls_score, labels, axis=1)
    loss_box = user_function(SmoothL1Loss(bbox_pred, bbox_targets, bbox_inside_weights))
    detection_losses = reduce_sum(loss_cls) + reduce_sum(loss_box)

    loss = rpn_losses + detection_losses
    pred_error = classification_error(cls_score, labels, axis=1)

    return cls_score, loss, pred_error

def create_eval_model(model, image_input):
    # modify Faster RCNN model by excluding target layers and losses
    feature_node = find_by_name(model, feature_node_name)
    conv_node = find_by_name(model, last_conv_node_name)
    rpn_roi_node = find_by_name(model, "rpn_rois")
    rpn_target_roi_node = find_by_name(model, "rpn_target_rois")
    cls_score_node = find_by_name(model, "cls_score")
    bbox_pred_node = find_by_name(model, "bbox_regr")

    conv_rpn_layers = combine([conv_node.owner, rpn_roi_node.owner])\
        .clone(CloneMethod.freeze, {feature_node: placeholder()})
    roi_fc_layers = combine([cls_score_node.owner, bbox_pred_node.owner])\
        .clone(CloneMethod.clone, {conv_node: placeholder(), rpn_target_roi_node: placeholder()})

    conv_rpn_net = conv_rpn_layers(image_input)
    conv_out = conv_rpn_net.outputs[0]
    rpn_rois = conv_rpn_net.outputs[1]

    pred_net = roi_fc_layers(conv_out, rpn_rois)
    cls_score = pred_net.outputs[0]
    bbox_regr = pred_net.outputs[1]

    cls_pred = softmax(cls_score, axis=1, name='cls_pred')
    return combine([cls_pred, rpn_rois, bbox_regr])

def train_model(image_input, roi_input, loss, pred_error,
                lr_schedule, mm_schedule, l2_reg_weight, epochs_to_train):
    if isinstance(loss, cntk.Variable):
        loss = combine([loss])
    # Instantiate the trainer object
    learner = momentum_sgd(loss.parameters, lr_schedule, mm_schedule, l2_regularization_weight=l2_reg_weight)
    trainer = Trainer(None, (loss, pred_error), learner)

    # Create the minibatch source
    minibatch_source = create_mb_source(train_map_file, train_roi_file,
        image_height, image_width, num_channels, num_input_rois)

    # define mapping from reader streams to network inputs
    input_map = {
        image_input: minibatch_source[features_stream_name],
        roi_input: minibatch_source[roi_stream_name]
    }

    # Get minibatches of images and perform model training
    print("Training model for %s epochs." % epochs_to_train)
    log_number_of_parameters(loss)
    progress_printer = ProgressPrinter(tag='Training', num_epochs=epochs_to_train)
    for epoch in range(epochs_to_train):       # loop over epochs
        sample_count = 0
        while sample_count < epoch_size:  # loop over minibatches in the epoch
            data = minibatch_source.next_minibatch(min(mb_size, epoch_size-sample_count), input_map=input_map)
            trainer.train_minibatch(data)                                    # update model with it
            sample_count += trainer.previous_minibatch_sample_count          # count samples processed so far
            progress_printer.update_with_trainer(trainer, with_metric=True)  # log progress
            if sample_count % 100 == 0:
                print("Processed {} samples".format(sample_count))

        progress_printer.epoch_summary(with_metric=True)

# Trains a Faster R-CNN model end-to-end
def train_faster_rcnn_e2e(debug_output=False):
    # Input variables denoting features and labeled ground truth rois (as 5-tuples per roi)
    image_input = input((num_channels, image_height, image_width), dynamic_axes=[Axis.default_batch_axis()], name=feature_node_name)
    roi_input   = input((num_input_rois, 5), dynamic_axes=[Axis.default_batch_axis()])

    # Instantiate the Faster R-CNN prediction model and loss function
    predictions, loss, pred_error = faster_rcnn_predictor(image_input, roi_input)

    if debug_output:
        print("Storing graphs and models to %s." % output_path)
        plot(loss, os.path.join(output_path, "graph_frcn_train_e2e." + graph_type))

    # Set learning parameters
    # Caffe Faster R-CNN parameters are:
    #   base_lr: 0.001
    #   lr_policy: "step"
    #   gamma: 0.1
    #   stepsize: 50000
    #   momentum: 0.9
    #   weight_decay: 0.0005
    # ==> CNTK: lr_per_sample = [0.001] * 10 + [0.0001] * 10 + [0.00001]
    l2_reg_weight = 0.0005
    lr_per_sample = [0.001] * 10 + [0.0001] * 10 + [0.00001]
    #lr_per_sample = [0.0005] * 10 + [0.0001] * 10 + [0.00001] * 10
    lr_schedule = learning_rate_schedule(lr_per_sample, unit=UnitType.sample)
    mm_schedule = momentum_schedule(0.9)

    train_model(image_input, roi_input, loss, pred_error,
                lr_schedule, mm_schedule, l2_reg_weight, epochs_to_train=max_epochs)
    return loss

# Trains a Faster R-CNN model using 4-stage alternating training
def train_faster_rcnn_alternating(debug_output=False):
    '''
        4-Step Alternating Training scheme from the Faster R-CNN paper:
        
        # Create initial network, only rpn, without detection network
            # --> train only the rpn (and conv3_1 and up for VGG16)
            # lr = [0.001] * 12 + [0.0001] * 4, momentum = 0.9, weight decay = 0.0005 (cf. stage1_rpn_solver60k80k.pt)
        
        # Create full network, initialize conv layers with imagenet, fix rpn weights
            # --> train only detection network (and conv3_1 and up for VGG16)
            # lr = [0.001] * 6 + [0.0001] * 2, momentum = 0.9, weight decay = 0.0005 (cf. stage1_fast_rcnn_solver30k40k.pt)
        
        # Keep conv weights from detection network and fix them
            # --> train only rpn
            # lr = [0.001] * 12 + [0.0001] * 4, momentum = 0.9, weight decay = 0.0005 (cf. stage2_rpn_solver60k80k.pt)
        
        # Keep conv and rpn weights from stpe 3 and fix them
            # --> train only detection netwrok
            # lr = [0.001] * 6 + [0.0001] * 2, momentum = 0.9, weight decay = 0.0005 (cf. stage2_fast_rcnn_solver30k40k.pt)
    '''

    # Learning parameters
    l2_reg_weight = 0.0005
    if base_model_to_use == "VGG16":
        mm_schedule = momentum_schedule(0.9)
    else:
        mm_schedule = momentum_schedule(0.5)

    # rpn training: lr = [0.001] * 12 + [0.0001] * 4, momentum = 0.9, weight decay = 0.0005 (cf. stage1_rpn_solver60k80k.pt)
    if base_model_to_use == "VGG16":
        rpn_epochs = 16
        lr_per_sample_rpn = [0.001] * 12 + [0.0001] * 4
    else:
        rpn_epochs = 16
        lr_per_sample_rpn = [0.002] * 4 + [0.001] * 4 + [0.0005] * 4 + [0.0001] * 4
    if start_train_conv_node_name != None:
        # TODO: this should be handled through different learning rates for the conv layers only
        # This is needed due to adding up all gradient in the ROI-pooling layer.
        # The below learning rates are then too small for the other layers and yield bad results.
        lr_per_sample_rpn =  [x * 0.01 for x in lr_per_sample_rpn]
    lr_schedule_rpn = learning_rate_schedule(lr_per_sample_rpn, unit=UnitType.sample)

    # frcn training: lr = [0.001] * 6 + [0.0001] * 2, momentum = 0.9, weight decay = 0.0005 (cf. stage1_fast_rcnn_solver30k40k.pt)
    if base_model_to_use == "VGG16":
        frcn_epochs = 20 #8
        lr_per_sample_frcn = [0.001] * 6 + [0.0001] * 2
    else:
        frcn_epochs = 20
        lr_per_sample_frcn = [0.0002] * 8 + [0.001] * 6 + [0.00001] * 6
    if start_train_conv_node_name != None:
        # TODO: this should be handled through different learning rates for the conv layers only
        # This is needed due to adding up all gradient in the ROI-pooling layer.
        # The below learning rates are then too small for the other layers and yield bad results.
        lr_per_sample_frcn =  [x * 0.01 for x in lr_per_sample_frcn]
    lr_schedule_frcn = learning_rate_schedule(lr_per_sample_frcn, unit=UnitType.sample)

    if debug_output:
        print("Storing graphs and models to %s." % output_path)
        print("Using base model: {}".format(base_model_to_use))
        print("lr_per_sample_rpn: {}".format(lr_per_sample_rpn))
        print("lr_per_sample_frcn: {}".format(lr_per_sample_frcn))

    # base image classification model (e.g. VGG16 or AlexNet)
    base_model = load_model(base_model_file)

    # stage 1a: rpn
    if True:
        # Create initial network, only rpn, without detection network
            #       initial weights     train?
            # conv: base_model          only conv3_1 and up
            # rpn:  init new            yes
            # frcn: -                   -

        print("stage 1a - rpn")

        # Input variables denoting features and labeled ground truth rois (as 5-tuples per roi)
        image_input = input((num_channels, image_height, image_width), dynamic_axes=[Axis.default_batch_axis()], name=feature_node_name)
        roi_input = input((num_input_rois, 5), dynamic_axes=[Axis.default_batch_axis()], name='roi_input')

        # conv layers
        if start_train_conv_node_name == None:
            conv_layers = clone_model(base_model, [feature_node_name], [last_conv_node_name], clone_method=CloneMethod.freeze)
            conv_out = conv_layers(image_input)
        else:
            fixed_conv_layers = clone_model(base_model, [feature_node_name], [start_train_conv_node_name], clone_method=CloneMethod.freeze)
            train_conv_layers = clone_model(base_model, [start_train_conv_node_name], [last_conv_node_name], clone_method=CloneMethod.clone)
            # TODO: it would be nicer to use Sequential(), but then the node name cannot be found in subsequent cloning
            # conv_layers = Sequential(fixed_conv_layers, train_conv_layers)
            conv_out_f = fixed_conv_layers(image_input)
            conv_out = train_conv_layers(conv_out_f)
        #conv_out = conv_layers(image_input)

        # RPN
        rpn_rois, rpn_losses = create_rpn(conv_out, roi_input)

        stage1_rpn_network = combine([rpn_rois, rpn_losses])
        if debug_output: plot(stage1_rpn_network, os.path.join(output_path, "graph_frcn_train_stage1a_rpn." + graph_type))

        # train
        train_model(image_input, roi_input, rpn_losses, rpn_losses,
                    lr_schedule_rpn, mm_schedule, l2_reg_weight, epochs_to_train=rpn_epochs)

    # stage 1b: fast rcnn
    if True:
        # Create full network, initialize conv layers with imagenet, fix rpn weights
            #       initial weights     train?
            # conv: base_model          only conv3_1 and up
            # rpn:  stage1 rpn model    no
            # frcn: base_model + new    yes

        print("stage 1b - frcn")

        # Input variables denoting features and labeled ground truth rois (as 5-tuples per roi)
        image_input = input((num_channels, image_height, image_width), dynamic_axes=[Axis.default_batch_axis()], name=feature_node_name)
        roi_input = input((num_input_rois, 5), dynamic_axes=[Axis.default_batch_axis()], name='roi_input')

        # conv_layers
        if start_train_conv_node_name == None:
            conv_layers = clone_model(base_model, [feature_node_name], [last_conv_node_name], CloneMethod.freeze)
            conv_out = conv_layers(image_input)
        else:
            fixed_conv_layers = clone_model(base_model, [feature_node_name], [start_train_conv_node_name], CloneMethod.freeze)
            train_conv_layers = clone_model(base_model, [start_train_conv_node_name], [last_conv_node_name], CloneMethod.clone)
            # TODO: it would be nicer to use Sequential(), but then the node name cannot be found in subsequent cloning
            # conv_layers = Sequential(fixed_conv_layers, train_conv_layers)
            conv_out_f = fixed_conv_layers(image_input)
            conv_out = train_conv_layers(conv_out_f)
        # conv_out = conv_layers(image_input)

        # RPN
        rpn = clone_model(stage1_rpn_network, [last_conv_node_name, "roi_input"], ["rpn_rois", "rpn_losses"], CloneMethod.freeze)
        rpn_net = rpn(conv_out, roi_input)
        rpn_rois = rpn_net.outputs[0]
        rpn_losses = rpn_net.outputs[1] # required for training rpn in stage 2

        ptl = user_function(ProposalTargetLayer(rpn_rois, roi_input, num_classes=num_classes))
        rois = alias(ptl.outputs[0], name='rpn_target_rois')
        labels = alias(ptl.outputs[1], name='label_targets')
        bbox_targets = alias(ptl.outputs[2], name='bbox_targets')
        bbox_inside_weights = alias(ptl.outputs[3], name='bbox_inside_w')

        # Fast RCNN
        fc_layers = clone_model(base_model, [pool_node_name], [last_hidden_node_name], CloneMethod.clone)
        cls_score, bbox_pred = create_fast_rcnn_predictor(conv_out, rois, fc_layers)

        # loss functions
        loss_cls = cross_entropy_with_softmax(cls_score, labels, axis=1)
        loss_box = user_function(SmoothL1Loss(bbox_pred, bbox_targets, bbox_inside_weights))
        detection_losses = plus(reduce_sum(loss_cls), reduce_sum(loss_box), name="detection_losses")

        stage1_frcn_network = combine([rois, cls_score, bbox_pred, rpn_losses, detection_losses])
        if debug_output: plot(stage1_frcn_network, os.path.join(output_path, "graph_frcn_train_stage1b_frcn." + graph_type))

        train_model(image_input, roi_input, detection_losses, detection_losses,
                    lr_schedule_frcn, mm_schedule, l2_reg_weight, epochs_to_train=frcn_epochs)

    # stage 2a: rpn
    if True:
        # Keep conv weights from detection network and fix them
            #       initial weights     train?
            # conv: stage1 frcn model   no
            # rpn:  stage1 rpn model    yes
            # frcn: -                   -

        print("stage 2a - rpn")

        # Input variables denoting features and labeled ground truth rois (as 5-tuples per roi)
        image_input = input((num_channels, image_height, image_width), dynamic_axes=[Axis.default_batch_axis()], name=feature_node_name)
        roi_input = input((num_input_rois, 5), dynamic_axes=[Axis.default_batch_axis()], name='roi_input')

        # conv_layers
        conv_layers = clone_model(stage1_frcn_network, [feature_node_name], [last_conv_node_name], CloneMethod.freeze)
        conv_out = conv_layers(image_input)

        # RPN
        rpn = clone_model(stage1_rpn_network, [last_conv_node_name, "roi_input"], ["rpn_rois", "rpn_losses"], CloneMethod.clone)
        rpn_net = rpn(conv_out, roi_input)
        rpn_rois = rpn_net.outputs[0]
        rpn_losses = rpn_net.outputs[1]

        stage2_rpn_network = combine([rpn_rois, rpn_losses])
        if debug_output: plot(stage2_rpn_network, os.path.join(output_path, "graph_frcn_train_stage2a_rpn." + graph_type))

        # train
        train_model(image_input, roi_input, rpn_losses, rpn_losses,
                    lr_schedule_rpn, mm_schedule, l2_reg_weight, epochs_to_train=rpn_epochs)

    # stage 2b: fast rcnn
    if True:
        # Keep conv and rpn weights from step 3 and fix them
            #       initial weights     train?
            # conv: stage2 rpn model    no
            # rpn:  stage2 rpn model    no
            # frcn: stage1 frcn modle   yes                   -

        print("stage 2b - frcn")

        # Input variables denoting features and labeled ground truth rois (as 5-tuples per roi)
        image_input = input((num_channels, image_height, image_width), dynamic_axes=[Axis.default_batch_axis()], name=feature_node_name)
        roi_input = input((num_input_rois, 5), dynamic_axes=[Axis.default_batch_axis()], name='roi_input')

        # conv_layers
        conv_layers = clone_model(stage2_rpn_network, [feature_node_name], [last_conv_node_name], CloneMethod.freeze)
        conv_out = conv_layers(image_input)

        # RPN
        rpn = clone_model(stage2_rpn_network, [last_conv_node_name], ["rpn_rois"], CloneMethod.freeze)
        rpn_rois = rpn(conv_out)

        # Fast RCNN
        frcn = clone_model(stage1_frcn_network, [last_conv_node_name, "rpn_rois", "roi_input"],
                           ["cls_score", "bbox_regr", "rpn_target_rois", "detection_losses"], CloneMethod.clone)
        stage2_frcn_network = frcn(conv_out, rpn_rois, roi_input)
        detection_losses = stage2_frcn_network.outputs[3]

        if debug_output: plot(stage2_frcn_network, os.path.join(output_path, "graph_frcn_train_stage2b_frcn." + graph_type))

        train_model(image_input, roi_input, detection_losses, detection_losses,
                    lr_schedule_frcn, mm_schedule, l2_reg_weight, epochs_to_train=frcn_epochs)

    # return stage 2 model
    return stage2_frcn_network

def load_resize_and_pad(image_path, width, height, pad_value=114):
    img = cv2.imread(image_path)
    img_width = len(img[0])
    img_height = len(img)

    scale_w = img_width > img_height

    target_w = width
    target_h = height

    if scale_w:
        target_h = int(np.round(img_height * float(width) / float(img_width)))
    else:
        target_w = int(np.round(img_width * float(height) / float(img_height)))

    resized = cv2.resize(img, (target_w, target_h), 0, 0, interpolation=cv2.INTER_NEAREST)

    top = int(max(0, np.round((height - target_h) / 2)))
    left = int(max(0, np.round((width - target_w) / 2)))

    bottom = height - top - target_h
    right = width - left - target_w

    resized_with_pad = cv2.copyMakeBorder(resized, top, bottom, left, right,
                                          cv2.BORDER_CONSTANT, value=[pad_value, pad_value, pad_value])

    # tranpose(2,0,1) converts the image to the HWC format which CNTK accepts
    model_arg_rep = np.ascontiguousarray(np.array(resized_with_pad, dtype=np.float32).transpose(2, 0, 1))

    return resized_with_pad, model_arg_rep

def regress_rois(roi_proposals, roi_regression_factors, labels):
    for i in range(len(labels)):
        label = labels[i]
        if label > 0:
            deltas = roi_regression_factors[i:i+1,label*4:(label+1)*4]
            roi_coords = roi_proposals[i:i+1,:]

            regressed_rois = bbox_transform_inv(roi_coords, deltas)

            roi_proposals[i,:] = regressed_rois
    return roi_proposals

# Tests a Faster R-CNN model and plots images with detected boxes
def eval_faster_rcnn_plot(eval_model, num_images_to_plot, debug_output=False):
    # get image paths
    with open(test_map_file) as f:
        content = f.readlines()
    img_base_path = os.path.dirname(os.path.abspath(test_map_file))
    img_file_names = [os.path.join(img_base_path, x.split('\t')[1]) for x in content]

    # prepare model
    image_input = input((num_channels, image_height, image_width), dynamic_axes=[Axis.default_batch_axis()], name=feature_node_name)
    frcn_eval = eval_model(image_input)

    num_eval = min(num_test_images, num_images_to_plot)
    print("Evaluating Faster R-CNN model for %s images." % num_eval)
    results_base_path = os.path.join(output_path, dataset)
    for i in range(0, num_eval):
        imgPath = img_file_names[i]

        # evaluate single image
        _, cntk_img_input = load_resize_and_pad(imgPath, image_width, image_height)
        output = frcn_eval.eval({frcn_eval.arguments[0]: [cntk_img_input]})

        out_dict = dict([(k.name, k) for k in output])
        out_cls_pred = output[out_dict['cls_pred']][0]
        out_rpn_rois = output[out_dict['rpn_rois']][0]
        out_bbox_regr = output[out_dict['bbox_regr']][0]

        labels = out_cls_pred.argmax(axis=1)
        scores = out_cls_pred.max(axis=1).tolist()

        if debug_output:
            # plot results without final regression
            imgDebug = visualizeResultsFaster(imgPath, labels, scores, out_rpn_rois, 1000, 1000,
                                              classes, nmsKeepIndices=None, boDrawNegativeRois=True)
            imsave("{}/{}_{}".format(results_base_path, i, os.path.basename(imgPath)), imgDebug)

        # apply regression to bbox coordinates
        regressed_rois = regress_rois(out_rpn_rois, out_bbox_regr, labels)
        img = visualizeResultsFaster(imgPath, labels, scores, regressed_rois, 1000, 1000,
                                     classes, nmsKeepIndices=None, boDrawNegativeRois=True)
        imsave("{}/{}_regr_{}".format(results_base_path, i, os.path.basename(imgPath)), img)

def eval_faster_rcnn_mAP(eval_model, img_map_file, roi_map_file):
    image_input = input((num_channels, image_height, image_width), dynamic_axes=[Axis.default_batch_axis()], name=feature_node_name)
    roi_input   = input((num_input_rois, 5), dynamic_axes=[Axis.default_batch_axis()])
    frcn_eval = eval_model(image_input)

    # Create the minibatch source
    minibatch_source = create_mb_source(img_map_file, roi_map_file,
        image_height, image_width, num_channels, num_input_rois, randomize=False)

    # define mapping from reader streams to network inputs
    input_map = {
        image_input: minibatch_source[features_stream_name],
        roi_input: minibatch_source[roi_stream_name]
    }

    # all detections are collected into:
    #    all_boxes[cls][image] = N x 5 array of detections in
    #    (x1, y1, x2, y2, score)
    all_boxes = [[[] for _ in range(num_test_images)] for _ in range(num_classes)]

    # evaluate test images and write netwrok output to file
    print("Evaluating Faster R-CNN model for %s images." % num_test_images)
    for i in range(0, num_test_images):
        mb_data = minibatch_source.next_minibatch(1, input_map=input_map)
        keys = [k for k in mb_data if "features" not in str(k)]
        gt_row = mb_data[keys[0]].asarray()[0,0,:]
        gt_boxes = gt_row.reshape((num_input_rois, 5))
        gt_boxes = gt_boxes[np.where(gt_boxes[:,-1] > 0)]

        output = frcn_eval.eval(mb_data)
        out_dict = dict([(k.name, k) for k in output])
        out_cls_pred = output[out_dict['cls_pred']][0]                      # (300, 17)
        out_rpn_rois = output[out_dict['rpn_rois']][0]
        out_bbox_regr = output[out_dict['bbox_regr']][0]

        labels = out_cls_pred.argmax(axis=1)
        scores = out_cls_pred.max(axis=1).tolist()
        regressed_rois = regress_rois(out_rpn_rois, out_bbox_regr, labels)  # (300, 4)

        import pdb; pdb.set_trace()
        for j in range(1, num_classes):
            all_boxes[j][i] = \
                np.hstack((regressed_rois, out_cls_pred[:, j])) \
                    .astype(np.float32, copy=False)


# The main method trains and evaluates a Fast R-CNN model.
# If a trained model is already available it is loaded an no training will be performed.
if __name__ == '__main__':
    os.chdir(base_path)
    if not os.path.exists(os.path.join(abs_path, "Output")):
        os.makedirs(os.path.join(abs_path, "Output"))
    if not os.path.exists(os.path.join(abs_path, "Output", "Grocery")):
        os.makedirs(os.path.join(abs_path, "Output", "Grocery"))
    if not os.path.exists(os.path.join(abs_path, "Output", "Pascal")):
        os.makedirs(os.path.join(abs_path, "Output", "Pascal"))

    #caffe_model = r"C:\Temp\Yuxiao_20170428_converted_models\VGG16_Faster-RCNN_VOC.cntkmodel"
    #dummy = load_model(caffe_model)
    #plot(dummy, r"C:\Temp\Yuxiao_20170426_converted_models\VGG16_Faster-RCNN_VOC.pdf")
    #import pdb; pdb.set_trace()

    model_path = os.path.join(abs_path, "Output", "faster_rcnn_eval_{}_{}.model"
                              .format(base_model_to_use, "e2e" if train_e2e else "4stage"))

    # Train only if no model exists yet
    if os.path.exists(model_path) and make_mode:
        print("Loading existing model from %s" % model_path)
        eval_model = load_model(model_path)
    else:
        if train_e2e:
            trained_model = train_faster_rcnn_e2e(debug_output=DEBUG_OUTPUT)
        else:
            trained_model = train_faster_rcnn_alternating(debug_output=DEBUG_OUTPUT)

        # create and store eval model
        image_input = input((num_channels, image_height, image_width), dynamic_axes=[Axis.default_batch_axis()], name=feature_node_name)
        eval_model = create_eval_model(trained_model, image_input)
        eval_model.save(model_path)
        if DEBUG_OUTPUT:
            plot(eval_model, os.path.join(output_path, "graph_frcn_eval_{}_{}.{}"
                                          .format(base_model_to_use, "e2e" if train_e2e else "4stage", graph_type)))

        print("Stored eval model at %s" % model_path)

    # Evaluate the test set
    #eval_faster_rcnn_mAP(eval_model, test_map_file, test_roi_file)
    if DEBUG_OUTPUT:
        eval_faster_rcnn_plot(eval_model, num_images_to_plot=500, debug_output=True)

# Copyright (C) 2022 Intel Corporation
#
# SPDX-License-Identifier: MIT

from pathlib import Path
import os

import cv2 as cv
import numpy as np
import requests


class IFSFunction:
    def __init__(self, prev_x, prev_y):
        self.function = []
        self.xs, self.ys = [prev_x], [prev_y]
        self.select_function = []
        self.cum_proba = 0.0

    def set_param(self, params, proba, weights=None):
        if weights is not None:
            params = list(np.array(params) * np.array(weights))

        self.function.append(params)
        self.cum_proba += proba
        self.select_function.append(self.cum_proba)

    def calculate(self, iteration):
        rand = np.random.random(iteration)
        prev_x, prev_y = 0, 0
        next_x, next_y = 0, 0

        for i in range(iteration):
            for func_params, select_func in zip(self.function, self.select_function):
                a, b, c, d, e, f = func_params
                if rand[i] <= select_func:
                    next_x = prev_x * a + prev_y * b + e
                    next_y = prev_x * c + prev_y * d + f
                    break

            self.xs.append(next_x)
            self.ys.append(next_y)
            prev_x = next_x
            prev_y = next_y

    @staticmethod
    def process_nans(data):
        nan_index = np.where(np.isnan(data))
        extend = np.array(range(nan_index[0][0] - 100, nan_index[0][0]))
        delete_row = np.append(extend, nan_index)
        return delete_row

    def rescale(self, image_x, image_y, pad_x, pad_y):
        xs = np.array(self.xs)
        ys = np.array(self.ys)
        if np.any(np.isnan(xs)):
            delete_row = self.process_nans(xs)
            xs = np.delete(xs, delete_row, axis=0)
            ys = np.delete(ys, delete_row, axis=0)

        if np.any(np.isnan(ys)):
            delete_row = self.process_nans(ys)
            xs = np.delete(xs, delete_row, axis=0)
            ys = np.delete(ys, delete_row, axis=0)

        if np.min(xs) < 0.0:
            xs -= np.min(xs)
        if np.min(ys) < 0.0:
            ys -= np.min(ys)
        xmax, xmin = np.max(xs), np.min(xs)
        ymax, ymin = np.max(ys), np.min(ys)
        self.xs = np.uint16(xs / (xmax - xmin) * (image_x - 2 * pad_x) + pad_x)
        self.ys = np.uint16(ys / (ymax - ymin) * (image_y - 2 * pad_y) + pad_y)

    def draw(self, image_x, image_y, draw_point, pad_x=6, pad_y=6):
        self.rescale(image_x, image_y, pad_x, pad_y)
        image = np.zeros((image_x, image_y), dtype=np.uint8)
        for x, y in zip(self.xs, self.ys):
            if draw_point:
                image[x, y] = 127
            else:
                mask = '{:09b}'.format(np.random.randint(1, 512))
                patch = 127 * np.array(list(map(int, list(mask))), dtype=np.uint8).reshape(3, 3)
                image[x+1:x+4, y+1:y+4] = patch

        return image


def download_colorization_model(path):
    proto_name = os.path.join(path, 'colorization_deploy_v2.prototxt')
    model_name = os.path.join(path, 'colorization_release_v2.caffemodel')
    npy_name = os.path.join(path, 'pts_in_hull.npy')

    if not os.path.exists(proto_name):
        url = 'https://raw.githubusercontent.com/richzhang/colorization/caffe/colorization/models/'
        proto = requests.get(url + proto_name)
        open(proto_name, 'wb').write(proto.content)
    if not os.path.exists(model_name):
        url = 'http://eecs.berkeley.edu/~rich.zhang/projects/2016_colorization/files/demo_v2/'
        model = requests.get(url + model_name)
        open(model_name, 'wb').write(model.content)
    if not os.path.exists(npy_name):
        url = 'https://github.com/richzhang/colorization/raw/caffe/colorization/resources/'
        pts_in_hull = requests.get(url + npy_name)
        open(npy_name, 'wb').write(pts_in_hull.content)


def rgb2lab(frame):
    y_coeffs = np.array([0.212671, 0.715160, 0.072169], dtype=np.float32)
    frame = np.where(frame > 0.04045, np.power((frame + 0.055) / 1.055, 2.4), frame / 12.92)
    y = frame @ y_coeffs.T
    L = np.where(y > 0.008856, 116 * np.cbrt(y) - 16, 903.3 * y)
    return L


def colorize(frame, net):
    H_orig, W_orig = frame.shape[:2] # original image size
    if len(frame.shape) == 2 or frame.shape[-1] == 1:
        frame = np.tile(frame.reshape(H_orig, W_orig, 1), (1, 1, 3))

    frame = frame.astype(np.float32) / 255
    img_l = rgb2lab(frame) # get L from Lab image
    img_rs = cv.resize(img_l, (224, 224)) # resize image to network input size
    img_l_rs = img_rs - 50  # subtract 50 for mean-centering

    net.setInput(cv.dnn.blobFromImage(img_l_rs))
    ab_dec = net.forward()[0, :, :, :].transpose((1, 2, 0))

    ab_dec_us = cv.resize(ab_dec, (W_orig, H_orig))
    img_lab_out = np.concatenate((img_l[..., np.newaxis], ab_dec_us), axis=2) # concatenate with original image L
    img_bgr_out = np.clip(cv.cvtColor(img_lab_out, cv.COLOR_Lab2BGR), 0, 1)
    frame_normed = 255 * (img_bgr_out - img_bgr_out.min()) / (img_bgr_out.max() - img_bgr_out.min())
    frame_normed = np.array(frame_normed, dtype=np.uint8)
    return cv.resize(frame_normed, (H_orig, W_orig))


def augment(image):
    if np.random.random(1) >= 0.5:
        image = cv.flip(image, 1)

    if np.random.random(1) >= 0.5:
        image = cv.flip(image, 0)

    height, width = image.shape[:2]
    angle = np.random.uniform(-30, 30)
    rotate_matrix = cv.getRotationMatrix2D(center=(width / 2, height / 2), angle=angle, scale=1)
    image = cv.warpAffine(src=image, M=rotate_matrix, dsize=(width, height))

    image = fill_background(image)

    k_size = np.random.choice(list(range(1, 16, 2)))
    image = cv.GaussianBlur(image, (k_size, k_size), 0)
    return image


def fill_background(image):
    synthetic_background = Path(__file__).parent / 'synthetic_background.npy'
    imagenet_means = np.load(synthetic_background)
    class_id = np.random.randint(0, imagenet_means.shape[0])
    rows, cols = np.where(~np.any(image, axis=-1))  # background color = [0, 0, 0]
    image[rows, cols] = imagenet_means[class_id]
    return image

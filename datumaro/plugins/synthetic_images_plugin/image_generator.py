# Copyright (C) 2022 Intel Corporation
#
# SPDX-License-Identifier: MIT

from multiprocessing import Pool
from shutil import rmtree
from typing import List, Optional
import os

import cv2 as cv
import numpy as np

from datumaro.components.cli_plugin import CliPlugin
from datumaro.components.errors import SourceExistsError

from .utils import IFSFunction, augment, colorize, download_colorization_model


class ImageGenerator(CliPlugin):
    """
    ImageGenerator generates a 3-channel synthetic dataset with provided shapes.
    """

    @classmethod
    def build_cmdline_parser(cls, **kwargs):
        parser = super().build_cmdline_parser(**kwargs)
        parser.add_argument('-k', '--count', type=int, required=True,
            help="Number of images to generate")
        parser.add_argument('-o', '--output-dir', type=os.path.abspath, required=True,
            help="Path to the directory where dataset are saved")
        parser.add_argument('--height', default=None,
            help="Height for generated images")
        parser.add_argument('--width', default=None,
            help="Width for generated images")
        parser.add_argument('--overwrite', action='store_true',
            help="Overwrite existing files in the save directory")

        return parser

    def __init__(self, count: int, output_dir: str,
                 height: int, width: int, overwrite: bool = False):
        self._count = count
        self._output_dir = output_dir
        self._height = height
        self._width = width

        if os.path.isdir(output_dir) and os.listdir(output_dir):
            if overwrite:
                rmtree(output_dir)
                os.mkdir(output_dir)
            else:
                raise SourceExistsError(output_dir)
        elif not os.path.isdir(output_dir):
            os.mkdir(output_dir)

        np.random.seed(0)
        self._cpu_count = min(os.cpu_count(), self._count)
        self._weights = np.array([
            0.2, 1, 1, 1, 1, 1,
            0.6, 1, 1, 1, 1, 1,
            1, 1, 1, 1, 1, 1,
            1.4, 1, 1, 1, 1, 1,
            1.8, 1, 1, 1, 1, 1,
            1, 0.2, 1, 1, 1, 1,
            1, 0.6, 1, 1, 1, 1,
            1, 1.4, 1, 1, 1, 1,
            1, 1.8, 1, 1, 1, 1,
            1, 1, 0.2, 1, 1, 1,
            1, 1, 0.6, 1, 1, 1,
            1, 1, 1.4, 1, 1, 1,
            1, 1, 1.8, 1, 1, 1,
            1, 1, 1, 0.2, 1, 1,
            1, 1, 1, 0.6, 1, 1,
            1, 1, 1, 1.4, 1, 1,
            1, 1, 1, 1.8, 1, 1,
            1, 1, 1, 1, 0.2, 1,
            1, 1, 1, 1, 0.6, 1,
            1, 1, 1, 1, 1.4, 1,
            1, 1, 1, 1, 1.8, 1,
            1, 1, 1, 1, 1, 0.2,
            1, 1, 1, 1, 1, 0.6,
            1, 1, 1, 1, 1, 1.4,
            1, 1, 1, 1, 1, 1.8,
        ]).reshape(-1, 6)
        self._threshold = 0.2
        self._iterations = 200000
        self._num_of_points = None
        self._instances = None
        self._categories = None

    def generate_dataset(self) -> None:
        path = os.path.dirname(os.path.abspath(__file__))
        download_colorization_model(path)
        self._initialize_params()

        with Pool(processes=self._cpu_count) as pool:
            params = pool.map(self._generate_category, [1e-5] * self._categories)

        instances_weights = np.repeat(self._weights, self._instances, axis=0)
        weight_per_img = np.tile(instances_weights, (self._categories, 1))
        repeated_params = np.repeat(params, self._weights.shape[0] * self._instances, axis=0)
        repeated_params = repeated_params[:self._count]
        weight_per_img = weight_per_img[:self._count]
        assert weight_per_img.shape[0] == len(repeated_params) == self._count

        splits = min(self._cpu_count, self._count)
        params_per_proc = np.array_split(repeated_params, splits)
        weights_per_proc = np.array_split(weight_per_img, splits)

        generation_params = []
        offset = 0
        for param, w in zip(params_per_proc, weights_per_proc):
            indices = list(range(offset, offset + len(param)))
            offset += len(param)
            generation_params.append((param, w, indices))

        with Pool(processes=self._cpu_count) as pool:
            pool.starmap(self._generate_image_batch, generation_params)

    def _generate_image_batch(self, params: np.ndarray, weights: np.ndarray, indices: List[int]) -> None:
        path = os.path.dirname(os.path.abspath(__file__))
        proto = os.path.join(path, 'colorization_deploy_v2.prototxt')
        model = os.path.join(path, 'colorization_release_v2.caffemodel')
        npy = os.path.join(path, 'pts_in_hull.npy')
        pts_in_hull = np.load(npy).transpose().reshape(2, 313, 1, 1).astype(np.float32)
        net = cv.dnn.readNetFromCaffe(proto, model)
        net.getLayer(net.getLayerId('class8_ab')).blobs = [pts_in_hull]
        net.getLayer(net.getLayerId('conv8_313_rh')).blobs = [np.full([1, 313], 2.606, np.float32)]

        for i, param, w in zip(indices, params, weights):
            image = self._generator(param, self._iterations, self._height, self._width, draw_point=False, weight=w)
            color_image = colorize(image, net)
            aug_image = augment(color_image)
            cv.imwrite(os.path.join(self._output_dir, "{:06d}.png".format(i)), aug_image)

    @staticmethod
    def _generator(params: np.ndarray, iterations: int, height: int, width: int,
                   draw_point: bool = True, weight: Optional[np.ndarray] = None) -> np.ndarray:
        ifs_function = IFSFunction(prev_x=0.0, prev_y=0.0)
        for param in params:
            ifs_function.set_param(param[:6], param[6], weight)
        ifs_function.calculate(iterations)
        img = ifs_function.draw(height, width, draw_point)
        return img

    def _generate_category(self, eps: float, base_h: int = 512, base_w: int = 512) -> np.ndarray:
        pixels = -1
        while pixels < self._threshold:
            param_size = np.random.randint(2, 8)
            params = np.zeros((param_size, 7), dtype=np.float32)

            sum_proba = eps
            for i in range(param_size):
                a, b, c, d, e, f = np.random.uniform(-1.0, 1.0, 6)
                prob = abs(a * d - b * c)
                sum_proba += prob
                params[i] = a, b, c, d, e, f, prob
            params[:, 6] /= sum_proba

            fracral_img = self._generator(params, self._num_of_points, base_h, base_w)
            pixels = np.count_nonzero(fracral_img) / (base_h * base_w)
        return params

    def _initialize_params(self) -> None:
        default_img_size = 362 * 362
        points_coeff = max(1, int(np.round(self._height * self._width / default_img_size)))
        self._num_of_points = 100000 * points_coeff

        if self._count < len(self._weights):
            self._instances = 1
            self._categories = 1
            self._weights = self._weights[:self._count, :]
        else:
            self._instances = np.ceil(0.25 * self._count / self._weights.shape[0]).astype(int)
            self._categories = np.ceil(self._count / (self._instances * self._weights.shape[0])).astype(int)

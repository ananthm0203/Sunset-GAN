# -*- coding: utf-8 -*-
"""sunset_gan.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1F5WWcP_UNrsGIGAdc0S13Czd0BN2LR8G
"""

from tensorflow import GradientTape
from tensorflow import function as tf_func
from tensorflow.nn import compute_average_loss

from tensorflow.keras.models import Model, clone_model, load_model
from tensorflow.keras.layers import Input, Dense, Conv2D, Conv2DTranspose
from tensorflow.keras.layers import Reshape, Flatten, AveragePooling2D, Add, Multiply

from tensorflow.keras.regularizers import l2
from tensorflow.keras.losses import BinaryCrossentropy, Reduction
from tesnorflow.keras.metrics import Mean
from tensorflow.keras import backend as K

from tensorflow.distribute import MirroredStrategy, ReduceOp

from .util import *

from tqdm import trange

from scipy.optimize import brenth

from gcsfs import GCSFileSystem
from google.cloud import storage

from tensorflow.python.lib.io import file_io

import numpy as np
from matplotlib import pyplot as plt
import cv2

import os
import ast
from importlib import import_module

class DiscoGAN():
    
    def __init__(self, input_shape, **params):

        def convert_kwargs(kwgs):

            if kwgs is None:
                return {}

            kwgs_dict = {}

            for i in range(1, len(kwgs), 2):
                try:
                    kwgs_dict[kwgs[i-1]] = ast.literal_eval(kwgs[i])
                except ValueError:
                    kwgs_dict[kwgs[i-1]] = kwgs[i]
            
            return kwgs_dict

        def load_model_from_path(path, project_name=None, key=None):

            if path[:5] == 'gs://':
                if project_name is None:
                    fs = GCSFileSystem()
                else:
                    fs = GCSFileSystem(project_name)
                file = fs.open(path)
            else:
                file = path

            return load_model(file, custom_objects={'Swish': Swish, 'InstanceNormalization': InstanceNormalization})

        self.strategy = MirroredStrategy()
        print(f'Number of devices detected: {self.strategy.num_replicas_in_sync}')

        self.input_shape = input_shape
        self.latent_dims = params.get('latent_dims', 1000)

        self.key = params.get('key', None)
        self.project_name = params.get('project_name', None)

        if params['d_fp'] is not None:
            self.discriminator = load_model_from_path(params['d_fp'])
        else:
            with self.strategy.scope():
                self.discriminator = self.make_discriminator()
        
        reshape_dims = K.int_shape(self.discriminator.layers[-3].output)[1:]
        
        if params['g_fp'] is not None:
            self.generator = load_model_from_path(params['g_fp'])
        else:
            with self.strategy.scope():
                self.generator = self.make_generator(reshape_dims)
        
        d_lr = params.get('d_lr', 4e-4)
        g_lr = params.get('g_lr', 4e-4)

        with self.strategy.scope():
            self.d_optimizer = getattr(import_module('tensorflow.keras.optimizers'), params['d_opt'])
            self.d_optimizer = self.d_optimizer(d_lr, **convert_kwargs(params['d_opt_params']))
        
        with self.strategy.scope():
            self.g_optimizer = getattr(import_module('tensorflow.keras.optimizers'), params['g_opt'])
            self.g_optimizer = self.g_optimizer(g_lr, **convert_kwargs(params['g_opt_params']))

        if params['print_summaries']:
            print(self.discriminator.summary())
            print(self.generator.summary())

    def make_generator(self, reshape_dims):

        def se_conv(x, filters, k_size, strides, padding, reg):

            x = Conv2DTranspose(filters, k_size, strides, padding, kernel_regularizer=reg, bias_regularizer=reg, use_bias=True) (x)
            
            shortcut = x
            
            x = Swish(True) (x)
            x = InstanceNormalization() (x)
            
            x = Conv2DTranspose(filters, k_size, strides, padding, kernel_regularizer=reg, bias_regularizer=reg, use_bias=True) (x)
            x = InstanceNormalization() (x)
                
            se = AveragePooling2D(K.int_shape(x)[2]) (x)
            se = Conv2D(min(filters//16, 1), 1) (se)
            se = Swish() (se)
            se = Conv2D(filters, 1, activation='sigmoid') (se)
            
            x = Multiply() ([x, se])
                
            x = Add() ([x, shortcut])
            x = Swish(True) (x)
            
            return x
        
        g_input = Input(self.latent_dims)

        self.g_reg = l2()
        
        x = Dense(np.prod(reshape_dims), kernel_regularizer=self.g_reg, bias_regularizer=self.g_reg) (g_input)
        x = Swish(True) (x)
        
        x = Reshape(reshape_dims) (x)
        
        x = Conv2DTranspose(256, 3, strides=2, padding='same', kernel_regularizer=self.g_reg, bias_regularizer=self.g_reg) (x)
        x = Swish(True) (x)
        x = InstanceNormalization() (x)
        
        x = Conv2DTranspose(192, 3, strides=2, padding='same', kernel_regularizer=self.g_reg, bias_regularizer=self.g_reg) (x)
        x = Swish(True) (x)
        x = InstanceNormalization() (x)
        
        x = se_conv(x, 128, 3, 1, 'same', self.g_reg)
        x = se_conv(x, 86, 3, 1, 'same', self.g_reg)
        x = se_conv(x, 64, 3, 1, 'same', self.g_reg)

        x = Conv2DTranspose(32, 3, strides=2, padding='same', kernel_regularizer=self.g_reg, bias_regularizer=self.g_reg) (x)
        x = Swish(True) (x)
        x = InstanceNormalization() (x)

        x = Conv2DTranspose(16, 3, strides=2, padding='same', kernel_regularizer=self.g_reg, bias_regularizer=self.g_reg) (x)
        x = Swish(True) (x)
        x = InstanceNormalization() (x)
        
        g_output = Conv2D(self.input_shape[-1], 1, padding='same', activation='tanh') (x)

        return Model(g_input, g_output, name='Generator')
    
    def make_discriminator(self):
        
        d_input = Input(self.input_shape)

        self.d_reg = l2()
        
        x = Conv2D(16, 3, padding='same', kernel_regularizer=self.d_reg, bias_regularizer=self.d_reg) (d_input)
        x = Swish(True) (x)
        x = InstanceNormalization() (x)
        
        x = Conv2D(32, 3, strides=2, padding='same', kernel_regularizer=self.d_reg, bias_regularizer=self.d_reg) (x)
        x = Swish(True) (x)
        x = InstanceNormalization() (x)
        
        x = Conv2D(64, 3, strides=2, padding='same', kernel_regularizer=self.d_reg, bias_regularizer=self.d_reg) (x)
        x = Swish(True) (x)
        x = InstanceNormalization() (x)
        
        x = Conv2D(128, 3, strides=2, padding='same', kernel_regularizer=self.d_reg, bias_regularizer=self.d_reg) (x)
        x = Swish(True) (x)
        x = InstanceNormalization() (x)

        x = Conv2D(256, 3, strides=2, padding='same', kernel_regularizer=self.d_reg, bias_regularizer=self.d_reg) (x)
        x = Swish(True) (x)
        x = InstanceNormalization() (x)
        
        
        x = Flatten() (x)
        d_output = Dense(1, activation='sigmoid') (x)
        
        return Model(d_input, d_output, name='Discriminator')
    
    def train(self, data_dir, **hparams):

        print(f'Loading images from {data_dir}')
        X = load_npz(data_dir, self.project_name, self.key)

        epochs = hparams.get('epochs', 1)
        batch_size = hparams.get('batch_size', 128)

        global_batch_size = batch_size * self.strategy.num_replicas_in_sync

        d_reg_C = hparams.get('d_initial_reg', 1e-2)
        g_reg_C = hparams.get('g_inital_reg', 1e-2)

        self.d_reg.l2 = -d_reg_C
        self.g_reg.l2 = g_reg_C

        d_min_reg = hparams.get('d_min_reg', 1e-4)
        g_min_reg = hparams.get('g_min_reg', 1e-4)

        q_max = hparams.get('max_q_size', 25)
        inc = hparams.get('q_update_inc', 10)

        plot_dims = hparams.get('plot_dims', (1, 5))
        plot_dir = hparams.get('plot_dir', '')
        plot_tstep = hparams.get('plot_tstep', 1)

        if plot_dir != '' and not os.path.isdir(plot_dir):
            os.makedirs(plot_dir)

        X = X.transpose(0, 2, 1, 3) # Flip rows and cols
        X = X/127.5 - 1

        steps, r = divmod(X.shape[0], batch_size)
        steps += 1

        m = steps//q_max
        
        real = np.ones((batch_size, 1))
        real_remainder = np.ones((r, 1))
        fake = np.zeros((batch_size, 1))
        fake_remainder = np.zeros((r, 1))

        full_inds = np.arange(X.shape[0])
        
        epoch = 0
        t = 1
        d_queue = [self.discriminator]
        g_queue = [self.generator]

        gen_r_labels = np.zeros((len(d_queue), 1))

        mean_loss_k = brenth(lambda k: sum([np.e**(-k*x) for x in range(1, steps+1)])-1, 0, 3)

        with self.strategy.scope():

            d_loss_object = BinaryCrossentropy(from_logits=True, reduction=Reduction.None)
            g_loss_object = BinaryCrossentropy(from_logits=True, reduction=Reduction.None)

            d_loss_current = np.inf
            g_loss_current = np.inf
            
            def compute_loss(true, preds, loss_object):
                batch_loss = loss_object(true, preds)
                return compute_average_loss(batch_loss, global_batch_size=global_batch_size)

        def update_queue(queue, var, t, m, K, inc):

            if t == m:
                if len(queue) <= K:
                    del queue[-1]
                queue.append(var)
                m += inc
            else:
                queue[-1] = var
            
            return m, queue

        def disc_train_step(g_queue, noise_size, batch, r_labels, f_labels):

            nonlocal d_loss_current

            noise = np.random.normal(0, 1, (noise_size, self.latent_dims))

            ims_arr = []
            val_arr = []
            for gen in g_queue:
                ims_arr.extend(gen(noise))
                val_arr.extend(f_labels)
            
            ims_arr.extend(batch)
            val_arr.extend(r_labels)

            ims_arr = np.array(ims_arr)
            val_arr = np.array(val_arr)
            shuffle_unison(ims_arr, val_arr)

            with GradientTape() as d_tape:

                preds = self.discriminator(ims_arr)
                d_loss = compute_loss(val_arr, preds, d_loss_object)

            d_loss_current = d_loss

            d_grad = d_tape.gradient(d_loss, self.discriminator.trainable_weights)
            self.d_optimizer.apply_gradients(zip(d_grad, self.discriminator.trainable_weights))

            return d_loss

        def gen_train_step(d_queue, noise_size, gen_r_labels):

            nonlocal g_loss_current

            with GradientTape() as g_tape:

                gen_ims = self.generator(np.random.normal(0, 1, (noise_size, self.latent_dims)))

                preds = []
                for disc in d_queue:
                    preds.extend(disc(gen_ims))
                preds = K.stack(preds)

                g_loss = compute_loss(gen_r_labels, preds, g_loss_object)

            g_loss_current = g_loss

            g_grad = g_tape.gradient(g_loss, self.generator.trainable_weights)
            self.g_optimizer.apply_gradients(zip(g_grad, self.generator.trainable_weights))
            
            return g_loss

        @tf_func
        def dist_train_step(step_func, args):
            per_replica_losses = self.strategy.run(step_func, args=args)
            return self.strategy.reduce(ReduceOp.SUM, per_replica_losses, axis=None)

        while epoch < epochs:
            
            np.random.shuffle(full_inds)
            
            g_loss_total = 0
            d_loss_total = 0
            
            with trange(steps) as bprogbar:
                for i in bprogbar:

                    bprogbar.set_description(f'Epoch {epoch+1}/{epochs}')
                    
                    if i < steps-1:
                        batch = X[full_inds[i*batch_size:(i+1)*batch_size]]
                        r_labels = real
                        f_labels = fake
                        noise_size = batch_size
                    else:
                        batch = X[full_inds[-r:]]
                        r_labels = real_remainder
                        f_labels = fake_remainder
                        noise_size = r
                    
                    dist_train_step(disc_train_step, (g_queue, noise_size, batch, r_labels, f_labels))
                    dist_train_step(gen_train_step, (d_queue, noise_size, gen_r_labels))

                    m, d_queue = update_queue(d_queue, clone_model(self.discriminator), t, m, q_max, inc)
                    m, g_queue = update_queue(g_queue, clone_model(self.generator), t, m, q_max, inc)

                    if t == m and len(d_queue) < K:
                        gen_r_labels = np.zeros((len(d_queue), 1))

                    bprogbar.set_postfix(d_loss=f'{d_loss_current:.4f}', g_loss=f'{g_loss_current:.4f}')
                    
                    d_loss_total += d_loss_current * np.e**(-mean_loss_k*(steps-i))
                    g_loss_total += g_loss_current * np.e**(-mean_loss_k*(steps-i))

                    t += 1

                    self.d_reg.l2 = -max(d_min_reg, d_reg_C/np.sqrt(t))
                    self.g_reg.l2 = max(g_min_reg, g_reg_C/np.sqrt(t))
                    
            epoch += 1
            
            print(f'Timestep: {t}; Average D Loss: {d_loss_total/(steps):.4f}, Average G Loss: {g_loss_total/(steps):.4f}')
            if not (epoch+1) % plot_tstep:
                self.plot_ims(plot_dims, epoch, plot_dir)
                        
    def plot_ims(self, plot_dims, epoch, save_dir='', project_name=None, key=None):
        
        r, c = plot_dims
        
        noise = np.random.normal(0, 1, (r*c, self.latent_dims))
        gen_ims = self.generator.predict(noise)
        gen_ims = np.uint8(np.transpose((gen_ims + 1) * 127.5, (0, 2, 1, 3)))
        
        fig, axs = plt.subplots(r, c)
        
        two_d = c > 1 and r > 1
        
        for i in range(r*c):
            
            if two_d:
                ax = axs[i//r][i%c]
            else:
                ax = axs[i]
                
            ax.imshow(cv2.cvtColor(gen_ims[i], cv2.COLOR_BGR2RGB))
            ax.axis('off')

        if save_dir != '':

            if save_dir[:5] == 'gs://':
            
                bucket, path = save_dir[5:].split('/', 1)
                
                client = storage.Client(credentials=key)
                bucket = client.bucket(bucket, project_name)
                blob = bucket.blob(path)

                fig.savefig(f'epoch_{epoch}.png')

                with file_io.FileIO(f'epoch_{epoch}.png') as png:
                    blob.upload_from_file(png)

            else:   
            
                fig.savefig(os.path.join(save_dir, f'epoch_{epoch}.png'))

        plt.show(block=False)
        plt.pause(3)
        plt.close()

            
    def save_models(self, save_dir, project_name=None, key=None):
        
        if save_dir[:5] == 'gs://':
        
            bucket, path = save_dir[5:].split('/', 1)
            
            client = storage.Client(credentials=key)
            bucket = client.bucket(bucket, project_name)
            blob = bucket.blob(path)

            self.discriminator.save('discriminator.h5')
            self.generator.save('generator.h5')

            with file_io.FileIO('discriminator.h5') as d_h5, file_io.FileIO('generator.h5') as g_h5:
                blob.upload_from_file(d_h5)
                blob.upload_from_file(g_h5)

        else:

            if not os.path.isdir(save_dir):
                os.makedirs(save_dir)
        
            self.discriminator.save(os.path.join(save_dir, 'discriminator.h5'))
            self.generator.save(os.path.join(save_dir, 'generator.h5'))
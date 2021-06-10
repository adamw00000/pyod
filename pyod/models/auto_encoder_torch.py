# -*- coding: utf-8 -*-
"""Using AutoEncoder with Outlier Detection (PyTorch)
"""
# Author: Yue Zhao <zhaoy@cmu.edu>
# License: BSD 2 clause

from __future__ import division
from __future__ import print_function

import torch
from torch import nn

import numpy as np
from sklearn.utils import check_array
from sklearn.utils.validation import check_is_fitted

from .base import BaseDetector
from ..utils.torch_utility import get_activation_by_name
from ..utils.stat_models import pairwise_distances_no_broadcast


class PyODDataset(torch.utils.data.Dataset):
    """PyOD Dataset class for PyTorch Dataloader
    """

    def __init__(self, X, y=None, mean=None, std=None):
        super(PyODDataset, self).__init__()
        self.X = X
        self.mean = mean
        self.std = std

    def __len__(self):
        return self.X.shape[0]

    def __getitem__(self, idx):
        if torch.is_tensor(idx):
            idx = idx.tolist()
        sample = self.X[idx, :]

        if self.mean.any():
            sample = (sample - self.mean) / self.std

        return torch.from_numpy(sample), idx


class inner_autoencoder(nn.Module):
    def __init__(self,
                 n_features,
                 hidden_neurons=[128, 64],
                 dropout_rate=0.2,
                 batch_norm=True,
                 hidden_activation='relu'):
        super(inner_autoencoder, self).__init__()
        self.n_features = n_features
        self.dropout_rate = dropout_rate
        self.batch_norm = batch_norm
        self.hidden_activation = hidden_activation

        self.activation = get_activation_by_name(hidden_activation)

        self.layers_neurons_ = [self.n_features, *hidden_neurons]
        self.layers_neurons_decoder_ = self.layers_neurons_[::-1]
        self.encoder = nn.Sequential()
        self.decoder = nn.Sequential()

        for idx, layer in enumerate(self.layers_neurons_[:-1]):
            if batch_norm:
                self.encoder.add_module("batch_norm" + str(idx),
                                        nn.BatchNorm1d(
                                            self.layers_neurons_[idx]))
            self.encoder.add_module("linear" + str(idx),
                                    torch.nn.Linear(self.layers_neurons_[idx],
                                                    self.layers_neurons_[
                                                        idx + 1]))
            self.encoder.add_module(self.hidden_activation + str(idx),
                                    self.activation)
            self.encoder.add_module("dropout" + str(idx),
                                    torch.nn.Dropout(dropout_rate))

        for idx, layer in enumerate(self.layers_neurons_[:-1]):
            if batch_norm:
                self.decoder.add_module("batch_norm" + str(idx),
                                        nn.BatchNorm1d(
                                            self.layers_neurons_decoder_[idx]))
            self.decoder.add_module("linear" + str(idx), torch.nn.Linear(
                self.layers_neurons_decoder_[idx],
                self.layers_neurons_decoder_[idx + 1]))
            self.encoder.add_module(self.hidden_activation + str(idx),
                                    self.activation)
            self.decoder.add_module("dropout" + str(idx),
                                    torch.nn.Dropout(dropout_rate))

    def forward(self, x):
        # we could return the latent representation here after the encoder as the latent representation
        x = self.encoder(x)
        x = self.decoder(x)
        return x


class AutoEncoder(BaseDetector):
    def __init__(self,
                 hidden_neurons=None,
                 hidden_activation='relu',
                 batch_norm=True,
                 # loss='mse', 
                 # optimizer='adam',
                 learning_rate=1e-3,
                 epochs=100,
                 batch_size=32,
                 dropout_rate=0.2,
                 # l2_regularizer=0.1, 
                 weight_decay=1e-5,
                 # validation_size=0.1, 
                 preprocessing=True,
                 loss_fn=None,
                 # verbose=1, 
                 # random_state=None, 
                 contamination=0.1,
                 device=None):
        super(AutoEncoder, self).__init__(contamination=contamination)
        self.hidden_neurons = hidden_neurons
        self.hidden_activation = hidden_activation
        self.batch_norm = batch_norm
        self.learning_rate = learning_rate

        self.epochs = epochs
        self.batch_size = batch_size

        self.dropout_rate = dropout_rate
        self.weight_decay = weight_decay
        self.preprocessing = preprocessing

        if loss_fn is None:
            self.loss_fn = torch.nn.MSELoss()

        if device is None:
            self.device = torch.device(
                "cuda:0" if torch.cuda.is_available() else "cpu")
        else:
            self.device = device

        # default values
        if self.hidden_neurons is None:
            self.hidden_neurons = [128, 64]

        # self.verbose = verbose

    # noinspection PyUnresolvedReferences
    def fit(self, X, y=None):
        """Fit detector. y is ignored in unsupervised methods.

        Parameters
        ----------
        X : numpy array of shape (n_samples, n_features)
            The input samples.

        y : Ignored
            Not used, present for API consistency by convention.

        Returns
        -------
        self : object
            Fitted estimator.
        """
        # validate inputs X and y (optional)
        X = check_array(X)
        self._set_n_classes(y)

        n_samples, n_features = X.shape[0], X.shape[1]

        # conduct standardization if needed
        if self.preprocessing:
            self.mean, self.std = np.mean(X, axis=0), np.mean(X, axis=0)
            train_set = PyODDataset(X=X, mean=self.mean, std=self.std)

        else:
            train_set = PyODDataset(X=X)

        train_loader = torch.utils.data.DataLoader(train_set,
                                                   batch_size=self.batch_size,
                                                   shuffle=True)

        # initialize the model
        self.model = inner_autoencoder(
            n_features=n_features,
            hidden_neurons=self.hidden_neurons,
            dropout_rate=self.dropout_rate,
            batch_norm=self.batch_norm,
            hidden_activation=self.hidden_activation)

        # move to device and print model information
        self.model = self.model.to(self.device)
        print(self.model)

        # train the autoencoder to find the best one
        self._train_autoencoder(train_loader)

        self.model.load_state_dict(self.best_model_dict)
        self.decision_scores_ = self.decision_function(X)

        self._process_decision_scores()
        return self

    def _train_autoencoder(self, train_loader):
        """Internal function to train the autoencoder

        Parameters
        ----------
        train_loader : torch dataloader
            Train data.
        """
        optimizer = torch.optim.Adam(
            self.model.parameters(), lr=self.learning_rate,
            weight_decay=self.weight_decay)

        self.best_loss = float('inf')
        self.best_model_dict = None

        for epoch in range(self.epochs):
            overall_loss = []
            for data, data_idx in train_loader:
                data = data.to(self.device).float()
                loss = self.loss_fn(data, self.model(data))
                # print('epoch {epoch} '.format(epoch=epoch), loss.item())

                self.model.zero_grad()
                loss.backward()
                optimizer.step()
                overall_loss.append(loss.item())
            print('epoch {epoch}: training loss {train_loss} '.format(
                epoch=epoch, train_loss=np.mean(overall_loss)))

            # track the best model so far
            if np.mean(overall_loss) <= self.best_loss:
                # print("epoch {ep} is the current best; loss={loss}".format(ep=epoch, loss=np.mean(overall_loss)))
                self.best_loss = np.mean(overall_loss)
                self.best_model_dict = self.model.state_dict()

    def decision_function(self, X):
        """Predict raw anomaly score of X using the fitted detector.

        The anomaly score of an input sample is computed based on different
        detector algorithms. For consistency, outliers are assigned with
        larger anomaly scores.

        Parameters
        ----------
        X : numpy array of shape (n_samples, n_features)
            The training input samples. Sparse matrices are accepted only
            if they are supported by the base estimator.

        Returns
        -------
        anomaly_scores : numpy array of shape (n_samples,)
            The anomaly score of the input samples.
        """
        check_is_fitted(self, ['model', 'best_model_dict'])
        X = check_array(X)

        # note the shuffle may be true but should be False
        if self.preprocessing:
            dataset = PyODDataset(X=X, mean=self.mean, std=self.std)
        else:
            dataset = PyODDataset(X=X)

        dataloader = torch.utils.data.DataLoader(dataset,
                                                 batch_size=self.batch_size,
                                                 shuffle=False)
        # enable the evaluation mode
        self.model.eval()

        # construct the vector for holding the reconstruction error
        outlier_scores = np.zeros([X.shape[0], ])
        with torch.no_grad():
            for data, data_idx in dataloader:
                data_cuda = data.to(self.device).float()
                # this is the outlier score
                outlier_scores[data_idx] = pairwise_distances_no_broadcast(
                    data, self.model(data_cuda).cpu().numpy())

        return outlier_scores

"""
This file contains the definition of the SMPL model
"""
from __future__ import division

import torch
import torch.nn as nn
import numpy as np
try:
    import cPickle as pickle
except ImportError:
    import pickle

from .geometric_layers import rodrigues
import models.config as cfg
def to_np(array, dtype=np.float32):
    if 'scipy.sparse' in str(type(array)):
        array = array.todense()
    return np.array(array, dtype=dtype)
class SMPL(nn.Module):

    def __init__(self, model_file=cfg.SMPL_FILE,age="adult",kid_template_path=None):
        super(SMPL, self).__init__()
        with open(model_file, 'rb') as f:
            smpl_model = pickle.load(f, encoding='iso-8859-1')
        J_regressor = smpl_model['J_regressor'].tocoo()
        row = J_regressor.row
        col = J_regressor.col
        data = J_regressor.data
        i = torch.LongTensor([row, col])
        v = torch.FloatTensor(data)
        J_regressor_shape = [24, 6890]
        self.register_buffer('J_regressor', torch.sparse.FloatTensor(i, v, J_regressor_shape).to_dense())
        self.register_buffer('weights', torch.FloatTensor(smpl_model['weights']))
        self.register_buffer('posedirs', torch.FloatTensor(smpl_model['posedirs']))
        self.register_buffer('v_template', torch.FloatTensor(smpl_model['v_template']))
        # kids de tempalte buyiyang
        if age=="kid":
            v_template_smil2 = np.load(kid_template_path)
            v_template_smil = v_template_smil2-np.mean(v_template_smil2, axis=0)
            v_template_diff = np.expand_dims(v_template_smil - smpl_model['v_template'], axis=2)
            shapedirs = np.concatenate((smpl_model['posedirs'][:, :, :10], v_template_diff), axis=2)
            num_betas = 11
            shapedirs = shapedirs[:, :, :num_betas]
            # print(shapedirs.shape)
            # The shape components
            self.register_buffer('shapedirs', torch.FloatTensor(shapedirs))

        else:
            self.register_buffer('shapedirs', torch.FloatTensor(np.array(smpl_model['shapedirs'])))
        self.register_buffer('faces', torch.from_numpy(smpl_model['f'].astype(np.int64)))
        self.register_buffer('kintree_table', torch.from_numpy(smpl_model['kintree_table'].astype(np.int64)))
        id_to_col = {self.kintree_table[1, i].item(): i for i in range(self.kintree_table.shape[1])}
        self.register_buffer('parent', torch.LongTensor([id_to_col[self.kintree_table[0, it].item()] for it in range(1, self.kintree_table.shape[1])]))

        self.pose_shape = [24, 3]
        if age == "kid":
            self.beta_shape = [11]
        else:
            self.beta_shape = [10]
        self.translation_shape = [3]

        self.pose = torch.zeros(self.pose_shape)
        self.beta = torch.zeros(self.beta_shape)
        self.translation = torch.zeros(self.translation_shape)

        self.verts = None
        self.J = None
        self.R = None
        
        J_regressor_extra = torch.from_numpy(np.load(cfg.JOINT_REGRESSOR_TRAIN_EXTRA)).float()
        self.register_buffer('J_regressor_extra', J_regressor_extra)
        self.joints_idx = cfg.JOINTS_IDX
        # This is the h36m regressor used in Graph-CMR(https://github.com/nkolot/GraphCMR)
        self.register_buffer('h36m_regressor_cmr', torch.FloatTensor(np.load(cfg.JOINT_REGRESSOR_H36M)))
        self.register_buffer('lsp_regressor_cmr', torch.FloatTensor(np.load(cfg.JOINT_REGRESSOR_H36M))[cfg.H36M_TO_J14])

        # This is another lsp joints regressor, we use it for training and evaluation
        self.register_buffer('lsp_regressor_eval', torch.FloatTensor(np.load(cfg.LSP_REGRESSOR_EVAL)).permute(1, 0))

        # We hope the training and evaluation regressor for the lsp joints to be consistent,
        # so we replace parts of the training regressor used in Graph-CMR.
        train_regressor = torch.cat([self.J_regressor, self.J_regressor_extra], dim=0)
        train_regressor = train_regressor[[cfg.JOINTS_IDX]].clone()
        idx = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 18]
        train_regressor[idx] = self.lsp_regressor_eval
        self.register_buffer('train_regressor', train_regressor)

    def forward(self, pose, beta):
        device = pose.device
        batch_size = pose.shape[0]
        v_template = self.v_template[None, :]
        shapedirs = self.shapedirs.view(-1,beta.shape[1])[None, :].expand(batch_size, -1, -1)
        # print(beta)
        beta = beta[:, :, None]
        # print(shapedirs.shape, beta.shape)
        v_shaped = torch.matmul(shapedirs, beta).view(-1, 6890, 3) + v_template

        # batched sparse matmul not supported in pytorch
        J = []
        for i in range(batch_size):
            J.append(torch.matmul(self.J_regressor, v_shaped[i]))
        J = torch.stack(J, dim=0)
        # input it rotmat: (bs,24,3,3)
        if pose.ndimension() == 4:
            R = pose

        # input it rotmat: (bs,72)
        elif pose.ndimension() == 2:
            pose_cube = pose.view(-1, 3) # (batch_size * 24, 1, 3)
            R = rodrigues(pose_cube).view(batch_size, 24, 3, 3)
            R = R.view(batch_size, 24, 3, 3)

        I_cube = torch.eye(3)[None, None, :].to(device)
        # I_cube = torch.eye(3)[None, None, :].expand(theta.shape[0], R.shape[1]-1, -1, -1)
        lrotmin = (R[:,1:,:] - I_cube).view(batch_size, -1)
        posedirs = self.posedirs.view(-1,207)[None, :].expand(batch_size, -1, -1)
        v_posed = v_shaped + torch.matmul(posedirs, lrotmin[:, :, None]).view(-1, 6890, 3)
        J_ = J.clone()
        J_[:, 1:, :] = J[:, 1:, :] - J[:, self.parent, :]
        G_ = torch.cat([R, J_[:, :, :, None]], dim=-1)
        pad_row = torch.FloatTensor([0,0,0,1]).to(device).view(1,1,1,4).expand(batch_size, 24, -1, -1)
        G_ = torch.cat([G_, pad_row], dim=2)
        G = [G_[:, 0].clone()]
        for i in range(1, 24):
            G.append(torch.matmul(G[self.parent[i-1]], G_[:, i, :, :]))
        G = torch.stack(G, dim=1)

        rest = torch.cat([J, torch.zeros(batch_size, 24, 1).to(device)], dim=2).view(batch_size, 24, 4, 1)
        zeros = torch.zeros(batch_size, 24, 4, 3).to(device)
        rest = torch.cat([zeros, rest], dim=-1)
        rest = torch.matmul(G, rest)
        G = G - rest
        T = torch.matmul(self.weights, G.permute(1,0,2,3).contiguous().view(24,-1)).view(6890, batch_size, 4, 4).transpose(0,1)
        rest_shape_h = torch.cat([v_posed, torch.ones_like(v_posed)[:, :, [0]]], dim=-1)
        v = torch.matmul(T, rest_shape_h[:, :, :, None])[:, :, :3, 0]
        return v

    def get_joints(self, vertices):
        """
        This method is used to get the joint locations from the SMPL mesh
        Input:
            vertices: size = (B, 6890, 3)
        Output:
            3D joints: size = (B, 38, 3)
        """
        # print(self.J_regressor.shape)
        joints = torch.einsum('bik,ji->bjk', [vertices, self.J_regressor])
        joints_extra = torch.einsum('bik,ji->bjk', [vertices, self.J_regressor_extra])
        joints = torch.cat((joints, joints_extra), dim=1)
        joints = joints[:, cfg.JOINTS_IDX]
        return joints

    def get_root(self, vertices):
        """
        This method is used to get the root locations from the SMPL mesh
        Input:
            vertices: size = (B, 6890, 3)
        Output:
            3D joints: size = (B, 1, 3)
        """
        joints = torch.einsum('bik,ji->bjk', [vertices, self.J_regressor])
        return joints[:, 0:1, :]
    def get_train_joints(self, vertices):
            """
            This method is used to get the training 24 joint locations from the SMPL mesh
            Input:
                vertices: size = (B, 6890, 3)
            Output:
                3D joints: size = (B, 24, 3)
            """
            joints = torch.matmul(self.train_regressor[None, :], vertices)
            return joints

        # Get 14 lsp joints for the evaluation.
    def get_eval_joints(self, vertices):
        """
        This method is used to get the 14 eval joint locations from the SMPL mesh
        Input:
            vertices: size = (B, 6890, 3)
        Output:
            3D joints: size = (B, 14, 3)
        """
        joints = torch.matmul(self.lsp_regressor_eval[None, :], vertices)
        return joints

    def get_full_joints(self, vertices):
        """
        This method is used to get the joint locations from the SMPL mesh
        Input:
            vertices: size = (B, 6890, 3)
        Output:
            3D joints: size = (B, 38, 3)
        """
        joints = torch.einsum('bik,ji->bjk', [vertices, self.J_regressor])
        joints_extra = torch.einsum('bik,ji->bjk', [vertices, self.J_regressor_extra])
        joints = torch.cat((joints, joints_extra), dim=1)
        return joints

    # Get 14 lsp joints use the joint regressor provided by CMR.
    def get_lsp_joints(self, vertices):
        joints = torch.matmul(self.lsp_regressor_cmr[None, :], vertices)
        return joints


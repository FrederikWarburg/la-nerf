"""
Model for InstructNeRF2NeRF
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Tuple, Type

import torch
from nerfstudio.cameras.rays import RayBundle, RaySamples
from nerfstudio.field_components.field_heads import FieldHeadNames
from nerfstudio.field_components.spatial_distortions import SceneContraction
from nerfstudio.model_components.losses import interlevel_loss, distortion_loss
from nerfstudio.models.nerfacto import NerfactoModel, NerfactoModelConfig
from nerfstudio.model_components.renderers import UncertaintyRenderer

from la_nerf.la_nerf_field import LaNerfactoField


@dataclass
class LaNerfModelConfig(NerfactoModelConfig):
    """Configuration for the InstructNeRF2NeRFModel."""

    _target: Type = field(default_factory=lambda: LaNerfModel)

    # laplace backend
    laplace_backend: Literal["nnj", "backpack", "none"] = "nnj"

    # laplace method
    laplace_method: Literal["laplace", "linearized-laplace"] = "laplace"

    # number of samples for laplace
    laplace_num_samples: int = 100

    # hessian shape
    laplace_hessian_shape: Literal["diag", "kron", "full"] = "diag"

    # activation function
    act_fn: Literal["relu", "tanh", "elu"] = "tanh"

    # output activation function
    out_act_fn: Literal["softplus", "truncexp"] = "softplus"

    # online laplace
    online_laplace: bool = False

    # hessian update ema
    hessian_update_ema: bool = True

class LaNerfModel(NerfactoModel):
    """Model for InstructNeRF2NeRF."""

    config: LaNerfModelConfig

    def populate_modules(self):
        """Required to use L1 Loss."""
        super().populate_modules()

        if self.config.disable_scene_contraction:
            scene_contraction = None
        else:
            scene_contraction = SceneContraction(order=float("inf"))

        # Fields
        self.field = LaNerfactoField(
            self.scene_box.aabb,
            hidden_dim=self.config.hidden_dim,
            num_levels=self.config.num_levels,
            max_res=self.config.max_res,
            log2_hashmap_size=self.config.log2_hashmap_size,
            hidden_dim_color=self.config.hidden_dim_color,
            spatial_distortion=scene_contraction,
            num_images=self.num_train_data,
            use_average_appearance_embedding=self.config.use_average_appearance_embedding,
            appearance_embedding_dim=self.config.appearance_embed_dim,
            laplace_backend=self.config.laplace_backend,
            laplace_method=self.config.laplace_method,
            laplace_num_samples=self.config.laplace_num_samples,
            laplace_hessian_shape=self.config.laplace_hessian_shape,
            act_fn=self.config.act_fn,
            out_act_fn=self.config.out_act_fn,
            online_laplace=self.config.online_laplace,
            hessian_update_ema=self.config.hessian_update_ema,
        )

        self.renderer_uq = UncertaintyRenderer()

    def get_loss_dict(self, outputs, batch, metrics_dict=None):
        loss_dict = {}
        image = batch["image"].to(self.device)
        loss_dict["rgb_loss"] = self.rgb_loss(image, outputs["rgb"])

        if self.training:
            loss_dict[
                "interlevel_loss"
            ] = self.config.interlevel_loss_mult * interlevel_loss(
                outputs["weights_list"], outputs["ray_samples_list"]
            )
            assert metrics_dict is not None and "distortion" in metrics_dict
            loss_dict["distortion_loss"] = (
                self.config.distortion_loss_mult * metrics_dict["distortion"]
            )
            if self.config.predict_normals:
                # orientation loss for computed normals
                loss_dict[
                    "orientation_loss"
                ] = self.config.orientation_loss_mult * torch.mean(
                    outputs["rendered_orientation_loss"]
                )

                # ground truth supervision for normals
                loss_dict[
                    "pred_normal_loss"
                ] = self.config.pred_normal_loss_mult * torch.mean(
                    outputs["rendered_pred_normal_loss"]
                )

        return loss_dict

    def _get_outputs_nerfacto(self, ray_samples: RaySamples):
        field_outputs = self.field(
            ray_samples, compute_normals=self.config.predict_normals
        )
        weights = ray_samples.get_weights(field_outputs[FieldHeadNames.DENSITY])

        rgb = self.renderer_rgb(rgb=field_outputs[FieldHeadNames.RGB], weights=weights)
        depth = self.renderer_depth(weights=weights, ray_samples=ray_samples)
        accumulation = self.renderer_accumulation(weights=weights)

        outputs = {
            "rgb": rgb,
            "accumulation": accumulation,
            "depth": depth,
        }

        return field_outputs, outputs, weights

    def get_outputs(self, ray_bundle: RayBundle):
        ray_samples, weights_list, ray_samples_list = self.proposal_sampler(
            ray_bundle, density_fns=self.density_fns
        )
        ray_samples_list.append(ray_samples)

        field_outputs, outputs, weights = self._get_outputs_nerfacto(ray_samples)

        weights_list.append(weights)
        if self.training:
            outputs["weights_list"] = weights_list
            outputs["ray_samples_list"] = ray_samples_list
        for i in range(self.config.num_proposal_iterations):
            outputs[f"prop_depth_{i}"] = self.renderer_depth(
                weights=weights_list[i], ray_samples=ray_samples_list[i]
            )

        if "rgb_mu" in field_outputs:
            rgb_mu = self.renderer_rgb(rgb=field_outputs["rgb_mu"], weights=weights)
            outputs["rgb_mu"] = rgb_mu

        if "rgb_sigma" in field_outputs:
            rgb_sigma = self.renderer_uq(betas=field_outputs["rgb_sigma"].sum(-1, keepdim=True), weights=weights)
            outputs["rgb_sigma"] = rgb_sigma

        if "density_sigma" in field_outputs:
            density_sigma = self.renderer_uq(betas=field_outputs["density_sigma"].sum(-1, keepdim=True), weights=weights)
            outputs["density_sigma"] = density_sigma
            
        return outputs

    def get_metrics_dict(self, outputs, batch):
        metrics_dict = {}
        image = batch["image"].to(self.device)
        metrics_dict["psnr"] = self.psnr(outputs["rgb"], image)
        if self.training:
            metrics_dict["distortion"] = distortion_loss(outputs["weights_list"], outputs["ray_samples_list"])

        if self.training:
            metrics_dict["hessian/max"] = self.field.hessian.max()
            metrics_dict["hessian/min"] = self.field.hessian.min()
            metrics_dict["hessian/mean"] = self.field.hessian.mean()
            metrics_dict["hessian/median"] = self.field.hessian.median()
            metrics_dict["hessian/sum"] = self.field.hessian.sum()

            metrics_dict["hessian_density/max"] = self.field.density_hessian.max()
            metrics_dict["hessian_density/min"] = self.field.density_hessian.min()
            metrics_dict["hessian_density/mean"] = self.field.density_hessian.mean()
            metrics_dict["hessian_density/median"] = self.field.density_hessian.median()
            metrics_dict["hessian_density/sum"] = self.field.density_hessian.sum()

        return metrics_dict
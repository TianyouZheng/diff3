import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.modules import loss
from torch.optim import Adam
from scipy.spatial.transform import Rotation as R
from tqdm import tqdm
import numpy as np
import wandb
import math
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
from scipy.linalg import expm, logm

from utils import *

def extract(a, t, x_shape):
    """
    Extracts the tensor at the given time step.
    Args:
        a: A tensor contains the values of all time steps.
        t: The time step to extract.
        x_shape: The reference shape.
    Returns:
        The extracted tensor.
    """
    b, *_ = t.shape
    out = a.gather(-1, t)
    return out.reshape(b, *((1,) * (len(x_shape) - 1)))

def cosine_schedule(timesteps, s=0.008):
    """
    Defines the cosine schedule for the diffusion process
    Args:
        timesteps: The number of timesteps.
        s: The strength of the schedule.
    Returns:
        The computed alpha.
    """
    steps = timesteps + 1
    x = torch.linspace(0, steps, steps)
    alphas_cumprod = torch.cos(((x / steps) + s) / (1 + s) * torch.pi * 0.5) ** 2
    alphas_cumprod = alphas_cumprod / alphas_cumprod[0]
    alphas = alphas_cumprod[1:] / alphas_cumprod[:-1]
    return torch.clip(alphas, 0.001, 1)

def skew_symmetric(v):
    """Construct the skew-symmetric matrix S(v) from a vector v = (x,y,z)."""
    x, y, z = v[..., 0], v[..., 1], v[..., 2]
    zero = torch.zeros_like(x)
    return torch.stack([
        torch.stack([zero, -z, y], dim=-1),
        torch.stack([z, zero, -x], dim=-1),
        torch.stack([-y, x, zero], dim=-1)
    ], dim=-2)

def so3_exp_map(v):
    """
    Exponential map from so(3) to SO(3).

    v: (..., 3) batch of vectors in R^3 (tangent space).
    Returns: (..., 3, 3) batch of rotation matrices in SO(3).
    """
    theta = torch.norm(v, dim=-1, keepdim=True)
    theta_clamped = theta.clamp(min=1e-8)
    V = skew_symmetric(v)  # (B,L,3,3)
    B, L = v.shape[0], v.shape[1]
    I = torch.eye(3, device=v.device).expand(B,L,3,3)
    sin_theta = torch.sin(theta)
    cos_theta = torch.cos(theta)
    sin_div = (sin_theta / theta_clamped).unsqueeze(-1)
    term2 = sin_div * V
    one_minus_cos_div = ((1 - cos_theta) / (theta_clamped**2)).unsqueeze(-1)
    term3 = one_minus_cos_div * (V @ V)
    R = I + term2 + term3
    return R

def so3_log_map(R):
    """
    Logarithm map from SO(3) to so(3).

    R: (..., 3, 3) batch of rotation matrices in SO(3).
    Returns: (..., 3) batch of tangent vectors.
    """
    trace_R = R[...,0,0] + R[...,1,1] + R[...,2,2]
    cos_theta = (trace_R - 1.) / 2.
    cos_theta = cos_theta.clamp(-1+1e-7, 1-1e-7)
    theta = torch.acos(cos_theta)
    theta_clamped = theta.clamp(min=1e-8)

    R_T = R.transpose(-1,-2)
    Q = (R - R_T) / 2.0
    x = (Q[...,2,1] - Q[...,1,2]) / 2
    y = (Q[...,0,2] - Q[...,2,0]) / 2
    z = (Q[...,1,0] - Q[...,0,1]) / 2
    v = torch.stack([x, y, z], dim=-1)  # (B,L,3)

    sin_theta = torch.sin(theta_clamped)
    scale = (theta / sin_theta).unsqueeze(-1)
    v = v * scale
    return v

def so3_interpolate(x, y, gamma):
    """
    Geodesic interpolation on SO(3): 
    lambda(gamma, x) = exp(gamma log(x))
    
    Here we just scale the tangent vector of x by gamma.
    
    x: (..., 3, 3) rotation matrix
    y: not needed here if we just scale from identity to x.
    gamma: scalar or (...,) shape broadcastable factor
    """
    v = so3_log_map(x)  # (...,3)
    new_v = gamma * v
    return so3_exp_map(new_v)

def rotation_distance_loss(R_pred, R_true):
    """
    Compute the rotation distance loss between two batches of rotation matrices.

    Args:
        P: Predicted rotation matrices of shape (B, L, 3, 3).
        Q: True rotation matrices of shape (B, L, 3, 3).

    Returns:
        Loss value (mean rotation distance in radians).
    """
    Rt = R_pred.transpose(-1,-2)
    M = Rt @ R_true
    trace_val = M[...,0,0] + M[...,1,1] + M[...,2,2]
    # Clip for numerical stability
    cos_theta = (trace_val - 1.0)/2.0
    cos_theta = cos_theta.clamp(-1+1e-7, 1-1e-7)
    theta = torch.acos(cos_theta)  # (B,)
    return (theta**2).mean()


# Diffuser Class
class DDPM_Diff:
    def __init__(self, score_model, beta_start=0.1, beta_end=1.0, trans_scale=1.0, device="cuda", timesteps=30, seq_len=128):
        self.score_model = score_model
        self.beta_start = beta_start
        self.beta_end = beta_end
        self.trans_scale = trans_scale
        self.device = device
        self.num_timesteps = timesteps
        self.seq_len = seq_len

        ### Translation Euclidean diffusion scheduler
        def f(t):
            s = 0.008
            return torch.square(torch.cos(torch.Tensor([((t/self.num_timesteps + s) /(1+s)) * (torch.pi / 2)])))

        a_bar_0 = torch.Tensor([1]).to(self.device)
        self.a = torch.cat((a_bar_0, cosine_schedule(self.num_timesteps).to(self.device)))

        self.a_bars = torch.cumprod(self.a, dim=0)
        self.a[0] = f(0) / f(self.num_timesteps)

        self.x0_param1 = 1 / torch.sqrt(self.a_bars[1:])
        self.x0_param2 = torch.sqrt(1 - self.a_bars[1:])

        self.sigma = torch.sqrt(torch.Tensor([(1 - self.a_bars[t-1]) * (1-self.a[t]) / (1 - self.a_bars[t]) for t in range(1,self.num_timesteps + 1)]).to(self.device))

        self.mean_param1 = torch.Tensor([torch.sqrt(self.a[t]) * (1-self.a_bars[t-1]) / (1-self.a_bars[t]) for t in range(1,self.num_timesteps + 1)]).to(self.device)
        self.mean_param2 = torch.Tensor([torch.sqrt(self.a_bars[t-1]) * (1-self.a[t]) / (1-self.a_bars[t]) for t in range(1,self.num_timesteps + 1)]).to(self.device)
        self.a_bars = self.a_bars[1:]
        self.a = self.a[1:]

        self.q_param1 = torch.sqrt(self.a_bars)
        self.q_param2 = torch.sqrt(1 - self.a_bars)

        ### Rotation SO3 diffusion scheduler
        self.beta_t = torch.linspace(beta_start, beta_end, self.num_timesteps, device=device)
        self.alpha_t = 1.0 - self.beta_t
        self.alpha_bar_t = torch.cumprod(self.alpha_t, dim=0)

    def igso3_sample(self, shape, device):
        """
        Sample from IGSO(3), which we'll assume to be random rotation vectors in 3D space.
        
        Args:
            shape (torch.Size): Desired output shape, must include 3 as the last dimension.
            device (torch.device): The device to create the tensor on.
        
        Returns:
            torch.Tensor: Sampled rotation vectors with shape [..., 3].
        """
        # Sample random unit vectors in 3D space (can be viewed as sampling rotation axes)
        random_angles = torch.rand(*shape[:-1], 1, device=device) * 2 * math.pi  # Random angles between 0 and 2pi
        random_axis = torch.randn(*shape[:-1], 3, device=device)  # Random vectors in 3D
        random_axis = random_axis / torch.norm(random_axis, dim=-1, keepdim=True)  # Normalize to get unit vectors
        
        # Rotation vector is axis * angle (using random_angle as the angle for each axis)
        rotation_vectors = random_axis * random_angles  # Shape: [..., 3]
        return rotation_vectors

    def forward_process(self, x1, R0, t, trans_init=None, rot_init=None):
        """Forward diffusion process with different noise sources for x1 and x2."""
        B, L, _ = x1.shape

        # Euclidean Translation forward
        epsilon1 = torch.randn_like(x1) if trans_init is None else trans_init

        x1_t = extract(self.q_param1, t, x1.shape) * x1 + extract(self.q_param2, t, x1.shape) * epsilon1

        # SO3 Manifold forward with IGSO3 noise and rotation matrix multiply
        v0 = so3_log_map(R0)
        alpha_bar_t = extract(self.alpha_bar_t, t, v0.shape)
        alpha_bar_t_sqrt = torch.sqrt(alpha_bar_t)
        # Noise
        epsilon2 = torch.randn_like(v0)

        vt = alpha_bar_t_sqrt * v0 + torch.sqrt(1.0 - alpha_bar_t) * epsilon2
        Rt = so3_exp_map(vt)

        return (x1_t, epsilon1), (Rt, epsilon2)

    def compute_loss(self, x1, x2, t):
        """Compute the diffusion loss for x1 and x2."""
        B, L, _ = x1.shape
        (x1_t, epsilon1), (x2_t, epsilon2) = self.forward_process(x1 * self.trans_scale, x2, t)

        x2_t_flatten = x2_t.reshape(B, L, 9)

        if self.score_model.name == "Unet":
            x1_t = x1_t.transpose(1,2)
            x2_t_flatten = x2_t_flatten.transpose(1,2)

        # Predict scores using the score model
        predicted_score1, predicted_score2 = self.score_model(x1_t, x2_t_flatten, t)

        if self.score_model.name == "Unet":
            predicted_score1 = predicted_score1.transpose(1,2)
            predicted_score2 = predicted_score2.transpose(1,2)
            x1_t = x1_t.transpose(1,2)
            x2_t_flatten = x2_t_flatten.transpose(1,2)

        x0_1 = extract(self.x0_param1, t, x1_t.shape) * (x1_t - extract(self.x0_param2, t, x1_t.shape) * predicted_score1)
        
        # Loss for each stream
        loss1 = F.l1_loss(predicted_score1, epsilon1)
        loss_origin1 = F.l1_loss(x0_1, x1)

        # Compute rotational loss
        R_pred = so3_exp_map(predicted_score2)   # (B,3,3)
        R_true = so3_exp_map(epsilon2)
        loss2 = rotation_distance_loss(R_pred, R_true)
        loss_origin2 = 0

        # Average the two losses
        return loss1 + loss_origin1 + loss2 + loss_origin2, loss1, loss2, loss_origin1, loss_origin2

    def sample(self, shape, device, num_steps=30, trans_init=None, rot_init=None, trans_noise=None, rot_noise=None):
        """
        Sample both x1 and x2 from the reverse diffusion process.

        Args:
            shape (torch.Size): Desired shape of the output tensors (Batch, seq, dim).
            device (torch.device): The device to create the tensor on.
            num_steps (int): The number of diffusion steps to reverse.

        Returns:
            torch.Tensor, torch.Tensor: Two tensors containing the sampled values for x1 and x2.
        """
        with torch.no_grad():
            # Initialize both x1 and x2 with random noise
            B, L, _ = shape
            x1_t = torch.randn(*shape, device=device) if trans_init is None else trans_init # Random noise for x1 (from normal distribution)
            if rot_init is not None:
                x2_t = rot_init
            else:
                v_T = torch.randn(B,L,3, device=device)
                x2_t = so3_exp_map(v_T)

            # Reverse the diffusion process for both x1 and x2
            for t in range(num_steps-1, -1, -1):

                t_tensor = torch.full((shape[0],), t, device=device)

                if self.score_model.name == "Unet":
                    x1_t = x1_t.transpose(1,2)
                    x2_t = x2_t.reshape(B, L, 9)
                    x2_t = x2_t.transpose(1,2)

                # Predict scores using the score model
                predicted_score1, predicted_score2 = self.score_model(x1_t, x2_t, t_tensor)

                if self.score_model.name == "Unet":
                    predicted_score1 = predicted_score1.transpose(1,2)
                    predicted_score2 = predicted_score2.transpose(1,2)
                    x1_t = x1_t.transpose(1,2)
                    x2_t = x2_t.transpose(1,2)
                    x2_t = x2_t.reshape(B, L, 3, 3)

                if t > 0:
                    if trans_noise is not None:
                        noise = trans_noise[t]
                    else:
                        noise = torch.randn_like(x1_t).to(device)
                else:
                    noise = torch.zeros_like(x1_t).to(device)

                x0 = extract(self.x0_param1, t_tensor, x1_t.shape) * (x1_t - extract(self.x0_param2, t_tensor, x1_t.shape) * predicted_score1)
                x0 = torch.clamp(x0, min=-1, max=1)

                mean = extract(self.mean_param1, t_tensor, x1_t.shape) * x1_t + extract(self.mean_param2, t_tensor, x1_t.shape) * x0
                sigma = extract(self.sigma, t_tensor, x1_t.shape)

                x1_t = mean + sigma * noise

                ### SO3

                # Compute v_t = log(x_t)
                v_t = so3_log_map(x2_t)  # (B,3)

                # Reconstruct v_0:
                # v_0_pred = (v_t - sqrt(1 - alpha_bar_t[t]) * v_pred_noise) / sqrt(alpha_bar_t[t])
                alpha_bar_t_sqrt = torch.sqrt(self.alpha_bar_t[t])
                v_0_pred = (v_t - torch.sqrt(1 - self.alpha_bar_t[t]) * predicted_score2) / alpha_bar_t_sqrt

                # Approximate x_0 from v_0_pred
                x_0_approx = so3_exp_map(v_0_pred)

                # Compute mu_t(x_t, x_0):
                # µ_t = λ( (√α_{t-1} β_t / (1−α_bar_t)) , x_0_approx ) λ( (√(α_t(1−α_bar_{t-1})) / (1−α_bar_t)), x_t )
                # Handle t=0 case: x_{-1} doesn't exist
                if t > 0:
                    alpha_t_ = self.alpha_t[t]
                    beta_t_ = self.beta_t[t]
                    alpha_t_minus = self.alpha_t[t-1]
                    alpha_bar_t_minus = self.alpha_bar_t[t-1]

                    c1 = (torch.sqrt(alpha_bar_t_minus)*beta_t_) / (1 - self.alpha_bar_t[t])
                    c2 = (torch.sqrt(alpha_t_*(1 - alpha_bar_t_minus))) / (1 - self.alpha_bar_t[t])

                    def lambda_map(gamma, X):
                        vX = so3_log_map(X)
                        return so3_exp_map(gamma * vX)

                    part1 = lambda_map(c1, x_0_approx)
                    part2 = lambda_map(c2, x2_t)

                    mu_t = torch.matmul(part1, part2)  # (B,L,3,3)

                    if rot_noise is not None:
                        epsilon = rot_noise[t]
                    else:
                        epsilon = torch.randn_like(v_t)

                    v_mu = so3_log_map(mu_t)
                    v_t_minus = v_mu + torch.sqrt(beta_t_)*epsilon
                    x2_t = so3_exp_map(v_t_minus)
                else:
                    # t=0 means no more steps, just return x_0_approx
                    x2_t = x_0_approx

            x1_t = torch.clamp(x1_t, min=-1, max=1)

            return compose_se3(x2_t, x1_t / self.trans_scale)  # Return the sampled tensors

    def train(self, data_loader, optimizer, device, epochs=10, num_timesteps=30, log_wandb=False, project_name="dual_input_diffusion", run_name="Diffusion"):
        if log_wandb:
            wandb.init(project=project_name, name=run_name, config={"epochs": epochs, "learning_rate": optimizer.param_groups[0]['lr']})
            all_R3_images = []  # To store all scatter plots across epochs
            all_se3_images = []      # To store all SE(3) trajectory plots across epochs
            sample_shape = (1, self.seq_len, 3)
            sample_noise_t = torch.randn(*sample_shape, device=device)

            v_T = torch.randn(*sample_shape, device=device)
            sample_noise_r = so3_exp_map(v_T)
        
        generate_loop = epochs // 5
        for epoch in range(epochs):
            epoch_loss = 0.0
            epoch_translation_eps_loss = 0.0
            epoch_translation_x0_loss = 0.0
            epoch_rotation_eps_loss = 0.0
            
            with tqdm(data_loader, desc=f"Epoch {epoch + 1}/{epochs}", unit="batch") as pbar:
                for i, batch in enumerate(pbar):
                    batch = batch.to(device)
                    rotations = batch[:, :, :3, :3]  # [B, n, 3, 3]
                    translations = batch[:, :, :3, 3]  # [B, n, 3]

                    optimizer.zero_grad()
                    t = torch.randint(0, num_timesteps-1, (rotations.shape[0],), device=rotations.device)
                
                    # Compute the loss for x1 and x2
                    loss, trans_loss, rot_loss, trans_x0_loss, rot_x0_loss = self.compute_loss(translations * self.trans_scale, rotations, t)
                
                    loss.backward()
                    optimizer.step()

                    epoch_loss += loss.item()
                    epoch_translation_eps_loss += trans_loss.item()
                    epoch_translation_x0_loss += trans_x0_loss.item()
                    epoch_rotation_eps_loss += rot_loss.item()
                    if log_wandb:
                        wandb.log({"batch_loss": loss.item(), "Translation eps loss": trans_loss.item(), \
                                   "Rotation eps loss": rot_loss.item(), "Translation x0 loss": trans_x0_loss.item()})

                    pbar.set_postfix(loss=loss.item())

            avg_loss = epoch_loss / len(data_loader)
            avg_translation_eps_loss = epoch_translation_eps_loss / len(data_loader)
            avg_translation_x0_loss = epoch_translation_x0_loss / len(data_loader)
            avg_rotation_eps_loss = epoch_rotation_eps_loss / len(data_loader)
            if log_wandb:
                wandb.log({"epoch_loss": avg_loss, "epoch": epoch + 1,
                           "epoch_translation_eps_loss": avg_translation_eps_loss,
                           "epoch_translation_x0_loss": avg_translation_x0_loss,
                           "epoch_rotation_eps_loss": avg_rotation_eps_loss})

                if (epoch + 1) % generate_loop == 0:
                    generated_se3 = self.sample((1, self.seq_len, 3), device, num_timesteps, trans_init=sample_noise_t, rot_init=sample_noise_r)
                    
                    trajectory = np.array([se3[:, :3, 3].detach().cpu().numpy() for se3 in generated_se3])[0]
                    rotations = np.array([se3[:, :3, :3].detach().cpu().numpy() for se3 in generated_se3])[0]

                    # Plot scatter
                    R3_fig = plt.figure()
                    ax = R3_fig.add_subplot(111, projection='3d')
                    ax.plot(trajectory[:, 0], trajectory[:, 1], trajectory[:, 2], label="Trajectory", color="blue")
                    ax.set_title("Generated R3 Trajectory epoch="+str(epoch+1))
                    ax.set_xlabel("X")
                    ax.set_ylabel("Y")
                    ax.set_zlabel("Z")
                    R3_img = wandb.Image(R3_fig)
                    plt.close(R3_fig)

                    # Plot SE(3) trajectory with orientations
                    se3_fig = plt.figure(figsize=(12, 8))
                    ax = se3_fig.add_subplot(111, projection='3d')
                    ax.plot(trajectory[:, 0], trajectory[:, 1], trajectory[:, 2], label="Trajectory", color="blue", linewidth=2)
                    
                    # Add quivers
                    step = 5  # Reduce clutter by plotting quivers every 5 points
                    scale = 0.01
                    for i in range(0, len(trajectory) - 1, step):
                        R = rotations

                        # The columns of R represent the directions of the x, y, and z axes of the local frame
                        x_axis = R[:, 0]
                        y_axis = R[:, 1]
                        z_axis = R[:, 2]

                        # Plot the quivers for each axis
                        # X-axis (red)
                        ax.quiver(trajectory[0], trajectory[1], trajectory[2],
                                  x_axis[0], x_axis[1], x_axis[2],
                                  length=scale, color='r', linewidth=1.5, alpha=0.6)

                        # Y-axis (green)
                        ax.quiver(trajectory[0], trajectory[1], trajectory[2],
                                  y_axis[0], y_axis[1], y_axis[2],
                                  length=scale, color='g', linewidth=1.5, alpha=0.6)

                        # Z-axis (blue)
                        ax.quiver(trajectory[0], trajectory[1], trajectory[2],
                                  z_axis[0], z_axis[1], z_axis[2],
                                  length=scale, color='b', linewidth=1.5, alpha=0.6)

                    ax.set_title("Generated SE(3) Trajectory epoch="+str(epoch+1))
                    ax.set_xlabel("X")
                    ax.set_ylabel("Y")
                    ax.set_zlabel("Z")
                    ax.legend()
                    se3_img = wandb.Image(se3_fig)
                    plt.close(se3_fig)

                    all_R3_images.append(R3_img)
                    all_se3_images.append(se3_img)

            print(f"Epoch {epoch + 1}, Loss: {avg_loss:.4f}")

        if log_wandb:
            wandb.log({
                "All R3 Plots": all_R3_images,
                "All SE(3) Plots": all_se3_images
            })
            wandb.finish()

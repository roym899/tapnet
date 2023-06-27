"""Visualize TAPIR result on a video with rerun."""
import haiku as hk
import jax
import mediapy as media
import numpy as np
import rerun as rr
import tree

from tapnet import tapir_model
from tapnet.utils import transforms, viz_utils


def build_model(frames, query_points):
    """Compute point tracks and occlusions given frames and query points."""
    model = tapir_model.TAPIR()
    outputs = model(
        video=frames,
        is_training=False,
        query_points=query_points,
        query_chunk_size=64,
    )
    return outputs


def preprocess_frames(frames: np.ndarray):
    """Preprocess frames to model inputs.

    Args:
      frames: [num_frames, height, width, 3], [0, 255], np.uint8

    Returns:
      frames: [num_frames, height, width, 3], [-1, 1], np.float32
    """
    frames = frames.astype(np.float32)
    frames = frames / 255 * 2 - 1
    return frames


def postprocess_occlusions(occlusions, expected_dist):
    """Postprocess occlusions to boolean visible flag.

    Args:
      occlusions: [num_points, num_frames], [-inf, inf], np.float32
      expected_dist: [num_points, num_frames], [-inf, inf], np.float32

    Returns:
      visibles: [num_points, num_frames], bool
    """
    # visibles = occlusions < 0
    visibles = (1 - jax.nn.sigmoid(occlusions)) * (
        1 - jax.nn.sigmoid(expected_dist)
    ) > 0.5
    return visibles


def inference(frames, query_points):
    """Inference on one video.

    Args:
      frames: [num_frames, height, width, 3], [0, 255], np.uint8
      query_points: [num_points, 3], [0, num_frames/height/width], [t, y, x]

    Returns:
      tracks: [num_points, 3], [-1, 1], [t, y, x]
      visibles: [num_points, num_frames], bool
    """
    # Preprocess video to match model inputs format
    frames = preprocess_frames(frames)
    num_frames, height, width = frames.shape[0:3]
    query_points = query_points.astype(np.float32)
    frames, query_points = frames[None], query_points[None]  # Add batch dimension

    # Model inference
    rng = jax.random.PRNGKey(42)
    outputs, _ = model_apply(params, state, rng, frames, query_points)
    outputs = tree.map_structure(lambda x: np.array(x[0]), outputs)
    tracks, occlusions, expected_dist = (
        outputs["tracks"],
        outputs["occlusion"],
        outputs["expected_dist"],
    )

    # Binarize occlusions
    visibles = postprocess_occlusions(occlusions, expected_dist)
    return tracks, visibles


def sample_random_points(frame_max_idx, height, width, num_points):
    """Sample random points with (time, height, width) order."""
    y = np.random.randint(0, height, (num_points, 1))
    x = np.random.randint(0, width, (num_points, 1))
    t = np.random.randint(0, frame_max_idx + 1, (num_points, 1))
    points = np.concatenate((t, y, x), axis=-1).astype(np.int32)  # [num_points, 3]
    return points


def log_query(query_frame: np.ndarray, query_xys: np.ndarray) -> None:
    rr.set_time_seconds("timestamp", 0.0)
    rr.set_time_sequence("frameid", 0)
    rr.log_image("query_frame", query_frame)
    rr.log_points("query_frame/query_points", query_xys, radii=5)


def log_video(frames, fps) -> None:
    for i, frame in enumerate(frames):
        rr.set_time_seconds("timestamp", i * 1.0 / fps)
        rr.set_time_sequence("frameid", i)
        rr.log_image("current_frame", frame)


# TODO argparse this stuff
resize_height = 256
resize_width = 256
num_points = 20  # TODO optionally pick grid points from foreground mask
video_file = "./tennis-vest.mp4"

rr.init("track test", spawn=True)

model = hk.transform_with_state(build_model)
model_apply = jax.jit(model.apply)
checkpoint_path = "checkpoint/tapir_checkpoint.npy"
ckpt_state = np.load(checkpoint_path, allow_pickle=True).item()
params, state = ckpt_state["params"], ckpt_state["state"]

video = media.read_video(video_file)

log_video(video, video.metadata.fps)

height, width = video.shape[1:3]
resized_frames = media.resize_video(video, (resize_height, resize_width))
resized_query_tijs = sample_random_points(
    0, resized_frames.shape[1], resized_frames.shape[2], num_points
)  # t, row, col

original_query_xys = (
    resized_query_tijs[:, 2:0:-1]
    / np.array([resize_width, resize_height])
    * np.array([width, height])
)
log_query(video[0], original_query_xys)

tracks, visibles = inference(resized_frames, resized_query_tijs)

tracks = transforms.convert_grid_coordinates(
    tracks, (resize_width, resize_height), (width, height)
)
out_video = viz_utils.paint_point_track(video, tracks, visibles)
with media.VideoWriter(
    "./tennis-vest-out.mp4", out_video.shape[1:3], fps=video.metadata.fps
) as writer:
    for image in out_video:
        writer.add_image(image)

t = np.linspace(0, 5, 1000)

# xys = np.stack((np.cos(t), np.sin(t)), axis=-1)
# print(xys.shape)

# rr.init("track_vis", spawn=True)
# for t, xy in zip(t, xys):
#     rr.set_time_seconds("time", t)
#     rr.log_point("current_point", xy, radius=0.01, color=[0.9, 0.2, 0.2])
#     rr.log_point("track", xy, radius=0.005, color=[0.9, 0.2, 0.2])

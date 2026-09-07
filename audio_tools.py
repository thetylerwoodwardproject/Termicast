"""
audio_tools.py - ffmpeg/ffprobe wrappers: format conversion, clip extraction,
and the minimalist "editorial waveform" soundbite video.

The waveform video is NOT ffmpeg's built-in showwaves filter (that draws a
literal, mirrored PCM waveform -- bars/oscilloscope style, which is exactly
what this look avoids). Instead we compute a smoothed volume envelope from
the clip, animate a single hand-drawn-looking line across frames with PIL,
and pipe raw frames into ffmpeg alongside the original audio.
"""
import io
import json
import os
import random
import struct
import subprocess

BG_COLORS = {
    'RED': (255, 0, 0),
    'GREEN': (0, 255, 0),
    'BLUE': (0, 0, 255),
}


class AudioToolsError(Exception):
    pass


def _run(cmd):
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise AudioToolsError(
            f'Command failed ({proc.returncode}): {" ".join(cmd)}\n{proc.stderr.decode(errors="replace")[-2000:]}'
        )
    return proc


def ffprobe_duration(path):
    """Return the duration of a media file in seconds."""
    proc = _run([
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', path,
    ])
    try:
        return float(proc.stdout.decode().strip())
    except ValueError:
        raise AudioToolsError(f'Could not read duration for {path}')


def convert_audio(src, dest, bitrate='192k'):
    """Convert any ffmpeg-readable audio (WAV/FLAC/MP3/etc) to MP3."""
    os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)
    _run(['ffmpeg', '-y', '-i', src, '-vn', '-codec:a', 'libmp3lame',
          '-b:a', bitrate, dest])
    return dest


def extract_clip(src, dest, start, end, bitrate='192k'):
    """Extract [start, end) seconds from src into an MP3 at dest."""
    duration = max(0.1, end - start)
    os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)
    _run(['ffmpeg', '-y', '-i', src, '-ss', str(start), '-t', str(duration),
          '-codec:a', 'libmp3lame', '-b:a', bitrate, dest])
    return dest


def _decode_pcm_mono(path, sample_rate):
    """Decode any audio file to raw signed 16-bit mono PCM at sample_rate."""
    proc = _run(['ffmpeg', '-y', '-i', path, '-f', 's16le', '-acodec', 'pcm_s16le',
                 '-ac', '1', '-ar', str(sample_rate), 'pipe:1'])
    return proc.stdout


# ── Pure envelope math (no ffmpeg/PIL -- easy to unit test) ─────────────────

def pcm_to_rms_frames(pcm_bytes, sample_rate, fps):
    """Split raw s16le mono PCM into per-video-frame RMS values (0..~1)."""
    samples_per_frame = max(1, sample_rate // fps)
    bytes_per_frame = samples_per_frame * 2
    num_frames = max(1, len(pcm_bytes) // bytes_per_frame or 1)

    frames = []
    for i in range(num_frames):
        chunk = pcm_bytes[i * bytes_per_frame:(i + 1) * bytes_per_frame]
        if len(chunk) < 2:
            frames.append(0.0)
            continue
        count = len(chunk) // 2
        samples = struct.unpack(f'<{count}h', chunk[:count * 2])
        sum_sq = sum(s * s for s in samples)
        rms = (sum_sq / count) ** 0.5
        frames.append(rms / 32768.0)
    return frames


def normalize_series(values, ceiling_percentile=0.95):
    """Scale values to 0..1 using a high percentile as the ceiling so one
    loud spike doesn't flatten everything else."""
    if not values:
        return []
    sorted_vals = sorted(values)
    idx = min(len(sorted_vals) - 1, int(len(sorted_vals) * ceiling_percentile))
    ceiling = sorted_vals[idx] or max(sorted_vals) or 1.0
    if ceiling <= 0:
        return [0.0 for _ in values]
    return [min(1.0, v / ceiling) for v in values]


def smooth_series(values, alpha=0.35):
    """Exponential moving average so amplitude changes read as organic
    pulses instead of jittery per-frame jumps."""
    if not values:
        return []
    smoothed = [values[0]]
    for v in values[1:]:
        smoothed.append(alpha * v + (1 - alpha) * smoothed[-1])
    return smoothed


def build_point_offsets(num_points, num_frames, seed=0, drift=0.12):
    """A slow random walk per point, bounded to [-1, 1], giving each point
    along the line its own organic, slightly-imperfect wobble."""
    rng = random.Random(seed)
    offsets = [0.0] * num_points
    series = []
    for _ in range(num_frames):
        row = []
        for i in range(num_points):
            offsets[i] += rng.uniform(-drift, drift)
            offsets[i] = max(-1.0, min(1.0, offsets[i]))
            row.append(offsets[i])
        series.append(row)
    return series


# ── Frame rendering + video encode ───────────────────────────────────────────

def render_waveform_video(clip_audio_path, dest_mp4_path, video_config):
    """Render the 9:16 waveform video for a soundbite clip."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        raise AudioToolsError('Pillow is required for waveform video rendering: pip3 install Pillow')

    width = video_config.get('width', 1080)
    height = video_config.get('height', 1920)
    fps = video_config.get('fps', 24)
    bg_name = video_config.get('bgColor', 'GREEN').upper()
    bg = BG_COLORS.get(bg_name, BG_COLORS['GREEN'])
    line_hex = video_config.get('waveformColorHex', '#FF4500').lstrip('#')
    line_rgb = tuple(int(line_hex[i:i + 2], 16) for i in (0, 2, 4))
    padding_ratio = video_config.get('paddingRatio', 0.06)

    duration = ffprobe_duration(clip_audio_path)
    num_frames = max(1, int(round(duration * fps)))

    sample_rate = 22050
    pcm = _decode_pcm_mono(clip_audio_path, sample_rate)
    raw_frames = pcm_to_rms_frames(pcm, sample_rate, fps)
    # Resample raw_frames to exactly num_frames values
    if len(raw_frames) != num_frames:
        raw_frames = [
            raw_frames[min(len(raw_frames) - 1, int(i * len(raw_frames) / num_frames))]
            for i in range(num_frames)
        ]
    amplitude = smooth_series(normalize_series(raw_frames))

    num_points = 40
    pad_x = int(width * padding_ratio)
    center_y = height // 2
    max_amp_px = height * 0.10
    line_width = max(4, int(width * 0.005))

    point_offsets = build_point_offsets(num_points, num_frames)
    xs = [pad_x + (width - 2 * pad_x) * i / (num_points - 1) for i in range(num_points)]

    ffmpeg_cmd = [
        'ffmpeg', '-y',
        '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{width}x{height}', '-r', str(fps),
        '-i', 'pipe:0',
        '-i', clip_audio_path,
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '192k',
        '-shortest',
        dest_mp4_path,
    ]
    os.makedirs(os.path.dirname(dest_mp4_path) or '.', exist_ok=True)
    proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    try:
        for frame_idx in range(num_frames):
            img = Image.new('RGB', (width, height), bg)
            draw = ImageDraw.Draw(img)
            amp = amplitude[frame_idx] * max_amp_px
            offsets = point_offsets[frame_idx]
            points = [(xs[i], center_y + offsets[i] * amp) for i in range(num_points)]
            draw.line(points, fill=line_rgb, width=line_width, joint='curve')
            proc.stdin.write(img.tobytes())
    finally:
        proc.stdin.close()
        _, stderr = proc.communicate()

    if proc.returncode != 0:
        raise AudioToolsError(f'ffmpeg waveform encode failed:\n{stderr.decode(errors="replace")[-2000:]}')
    return dest_mp4_path

"""Single-frame V4L2 capture, invoked only by the explicit camera CLI option."""
import base64
import re
import shutil
import subprocess


class CaptureError(RuntimeError):
    pass


def capture_image(device, *, runner=subprocess.run, which=shutil.which):
    if not isinstance(device, str) or not re.fullmatch(r'/dev/video\d+', device):
        raise CaptureError('请指定实际彩色摄像头节点，例如 /dev/video0')
    executable = which('ffmpeg')
    if executable is not None:
        command = [executable, '-hide_banner', '-loglevel', 'error', '-f', 'video4linux2',
                   '-i', device, '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1']
    else:
        executable = which('gst-launch-1.0')
        if executable is None:
            raise CaptureError('此拍照入口需要ffmpeg或gst-launch-1.0；也可先用--image传入已有穿搭照片')
        command = [executable, '-q', 'v4l2src', f'device={device}', 'num-buffers=1',
                   '!', 'videoconvert', '!', 'jpegenc', '!', 'fdsink', 'fd=1']
    try:
        result = runner(command, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise CaptureError('摄像头未及时返回照片；请检查节点和占用，程序不会停止原厂服务') from None
    if result.returncode or not result.stdout or len(result.stdout) > 4 * 1024 * 1024:
        raise CaptureError('无法取得有效彩色照片；请检查摄像头节点或使用--image')
    return 'data:image/jpeg;base64,' + base64.b64encode(result.stdout).decode('ascii')

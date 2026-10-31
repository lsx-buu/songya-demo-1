"""Interactive local credential setup; importing this module never prompts."""
from pathlib import Path
import getpass

from outfit.config import Settings
from outfit.speech import VoiceConfig, DEFAULT_SPEAKER

DEFAULT_VISION_MODEL = 'doubao-seed-2-1-pro-260915'


def configure(root=None, *, input_fn=input, secret_fn=getpass.getpass):
    root = Path(root or Path(__file__).resolve().parent)
    settings = Settings(root)
    old_visual = settings.read()
    model = input_fn(f"视觉模型 [{old_visual['model'] or DEFAULT_VISION_MODEL}]：").strip()
    model = model or old_visual['model'] or DEFAULT_VISION_MODEL
    api_key = secret_fn('视觉 API Key（留空保留原值）：')
    settings.save(model, api_key)
    voice = VoiceConfig(root)
    old_voice = voice.read()
    app_id = secret_fn('语音 App ID（留空保留原值）：')
    access_key = secret_fn('语音 Access Key（留空保留原值）：')
    speaker = input_fn(f"语音音色 [{old_voice['speaker'] or DEFAULT_SPEAKER}]：").strip() or old_voice['speaker']
    voice.save(app_id, access_key, old_voice['model'], speaker)
    return {'vision': settings.public(), 'voice': voice.public()}


if __name__ == '__main__':
    print(configure())

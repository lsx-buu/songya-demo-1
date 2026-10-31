from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class ConfigError(ValueError):
    pass


class Settings:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.path = self.root / '.settings.json'

    def read(self) -> dict:
        if not self.path.exists():
            return {'model': '', 'api_key': ''}
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise ConfigError('本地设置文件无法读取，请重新保存设置') from error
        if not isinstance(value, dict):
            raise ConfigError('本地设置格式无效，请重新保存设置')
        return {'model': str(value.get('model', '')), 'api_key': str(value.get('api_key', ''))}

    def public(self) -> dict:
        value = self.read()
        return {'version': '0.1.0', 'configured': bool(value['model'] and value['api_key']),
                'model': value['model'], 'key_configured': bool(value['api_key'])}

    def save(self, model: str, api_key: str) -> dict:
        model = model.strip()
        if not model or len(model) > 160 or any(c in model for c in '\r\n'):
            raise ConfigError('模型标识不能为空，且不能超过160个字符')
        if '\r' in api_key or '\n' in api_key:
            raise ConfigError('API Key不能包含换行符')
        old = self.read()
        payload = {'model': model, 'api_key': api_key.strip() or old['api_key']}
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.settings.', dir=self.root)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return self.public()

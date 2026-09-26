"""Encrypted history markers shared with the local GPT router."""
import json

from cryptography.fernet import Fernet, InvalidToken


class ProtocolError(ValueError):
    pass


class CarrierCodec:
    # Preserve the prefix and key format so existing chat history remains readable.
    PREFIX = 'codex_claude_v1.'

    def __init__(self, key):
        self.cipher = Fernet(key)

    def encode(self, carrier):
        return self.PREFIX + self.cipher.encrypt(json.dumps(carrier).encode()).decode()

    def decode(self, value):
        if not value or not value.startswith(self.PREFIX):
            return None
        try:
            return json.loads(self.cipher.decrypt(value[len(self.PREFIX):].encode()))
        except (InvalidToken, ValueError):
            raise ProtocolError('Invalid native reasoning carrier') from None

"""
Система сохранения и восстановления состояния бота
"""

import base64
import json
import logging
import os
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

import config
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

log = logging.getLogger("persistence")


class StatePersistence:
    """Управление сохранением и восстановлением состояния"""

    def __init__(self, state_dir: str = None, user_data_dir: str = None):
        self.state_dir = state_dir or config.STATE_DIR
        self.user_data_dir = user_data_dir or config.USER_DATA_DIR
        os.makedirs(self.state_dir, exist_ok=True)
        os.makedirs(self.user_data_dir, exist_ok=True)

    def _get_user_file(self, user_id: int) -> str:
        """Получить путь к файлу состояния пользователя"""
        return os.path.join(self.state_dir, f"user_{user_id}.json")

    def save_state(self, user_id: int, state: Dict):
        """Сохранить состояние пользователя"""
        try:
            file_path = self._get_user_file(user_id)
            state["saved_at"] = datetime.now().isoformat()
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, default=str, ensure_ascii=False)
            log.info(f"State saved for user {user_id}")
        except (OSError, TypeError, ValueError) as e:
            log.error(f"Error saving state for user {user_id}: {e}")

    def load_state(self, user_id: int) -> Optional[Dict]:
        """Загрузить состояние пользователя"""
        try:
            file_path = self._get_user_file(user_id)
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                log.info(f"State loaded for user {user_id}")
                return state
        except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            log.error(f"Error loading state for user {user_id}: {e}")
        return None

    def list_user_ids_with_state(self) -> List[int]:
        """Список user_id, у которых есть файл состояния (для авто-восстановления ботов)."""
        result = []
        try:
            import re
            pattern = re.compile(r"^user_(\d+)\.json$")
            for name in os.listdir(self.state_dir):
                m = pattern.match(name)
                if m:
                    result.append(int(m.group(1)))
        except OSError as e:
            log.warning(f"Could not list state dir for auto-restore: {e}")
        return result

    def save_orders(self, user_id: int, orders: List[Dict]):
        """Сохранить ордера пользователя"""
        try:
            state = self.load_state(user_id) or {}
            state["orders"] = orders
            state["orders_saved_at"] = datetime.now().isoformat()
            self.save_state(user_id, state)
        except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            log.error(f"Error saving orders for user {user_id}: {e}")

    def load_orders(self, user_id: int) -> List[Dict]:
        """Загрузить ордера пользователя"""
        try:
            state = self.load_state(user_id)
            if state and "orders" in state:
                return state["orders"]
        except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            log.error(f"Error loading orders for user {user_id}: {e}")
        return []

    def delete_state(self, user_id: int):
        """Удалить состояние пользователя"""
        try:
            file_path = self._get_user_file(user_id)
            if os.path.exists(file_path):
                os.remove(file_path)
                log.info(f"State deleted for user {user_id}")
        except OSError as e:
            log.error(f"Error deleting state for user {user_id}: {e}")

    def _get_uid_file(self, uid: str) -> str:
        """Получить путь к файлу пользователя по UID (только цифры)"""
        # Убираем все нецифровые символы из UID для безопасности имени файла
        clean_uid = "".join(filter(str.isdigit, str(uid)))
        if not clean_uid:
            raise ValueError(f"UID должен содержать хотя бы одну цифру: {uid}")
        return os.path.join(self.user_data_dir, f"{clean_uid}.json")

    def save_user_trades(self, uid: str, trades: List[Dict], settings: Dict = None):
        """Сохранить все сделки и настройки пользователя в файл по UID
        ВАЖНО: Эта функция сохраняет только trades и settings, не перезаписывает api_keys!
        """
        try:
            file_path = self._get_uid_file(uid)
            # Загружаем существующие данные, чтобы не потерять api_keys
            existing_data = self.load_user_trades(uid)
            user_data = {
                "uid": uid,
                "trades": trades,
                "settings": settings or existing_data.get("settings", {}),
                "last_updated": datetime.now().isoformat(),
                "total_trades": len(trades),
            }
            # Сохраняем api_keys, если они есть
            if "api_keys" in existing_data:
                user_data["api_keys"] = existing_data["api_keys"]
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(user_data, f, indent=2, default=str, ensure_ascii=False)
            log.info(f"User trades and settings saved for UID {uid} ({len(trades)} trades)")
        except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            log.error(f"Error saving user trades for UID {uid}: {e}")

    def load_user_trades(self, uid: str) -> Dict:
        """Загрузить все сделки и настройки пользователя из файла по UID"""
        try:
            file_path = self._get_uid_file(uid)
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8") as f:
                    user_data = json.load(f)
                log.info(f"User trades and settings loaded for UID {uid} ({user_data.get('total_trades', 0)} trades)")
                return user_data
        except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            log.error(f"Error loading user trades for UID {uid}: {e}")
        return {"uid": uid, "trades": [], "settings": {}, "total_trades": 0}

    def add_trade(self, uid: str, trade: Dict):
        """Добавить одну сделку в файл пользователя"""
        try:
            user_data = self.load_user_trades(uid)
            if "timestamp" not in trade:
                trade["timestamp"] = datetime.now().isoformat()
            user_data["trades"].append(trade)
            user_data["total_trades"] = len(user_data["trades"])
            user_data["last_updated"] = datetime.now().isoformat()
            self.save_user_trades(uid, user_data["trades"], user_data.get("settings", {}))
        except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            log.error(f"Error adding trade for UID {uid}: {e}")

    def save_user_settings(self, uid: str, settings: Dict):
        """Сохранить настройки пользователя"""
        try:
            user_data = self.load_user_trades(uid)
            user_data["settings"].update(settings)
            user_data["last_updated"] = datetime.now().isoformat()
            self.save_user_trades(uid, user_data["trades"], user_data["settings"])
        except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            log.error(f"Error saving user settings for UID {uid}: {e}")

    def _get_encryption_key(self, uid: str) -> bytes:
        """Получить ключ шифрования для UID"""
        secret_key = getattr(config, "ENCRYPTION_SECRET", "") or ""
        if not secret_key:
            raise ValueError("ENCRYPTION_SECRET обязателен. Задайте в .env.")
        password = f"{uid}_{secret_key}".encode()
        salt = b"AutoScaleX_Salt_2024"  # фиксированная соль (смена сломает расшифровку существующих ключей)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(password))
        return key

    def encrypt_api_keys(self, uid: str, api_key: str, api_secret: str) -> Tuple[str, str]:
        """Зашифровать API ключи для UID"""
        try:
            key = self._get_encryption_key(uid)
            f = Fernet(key)
            encrypted_key = f.encrypt(api_key.encode()).decode()
            encrypted_secret = f.encrypt(api_secret.encode()).decode()
            return encrypted_key, encrypted_secret
        except (ValueError, TypeError, OSError) as e:
            log.error(f"Error encrypting API keys for UID {uid}: {e}")
            raise

    def decrypt_api_keys(self, uid: str, encrypted_key: str, encrypted_secret: str) -> Tuple[str, str]:
        """Расшифровать API ключи для UID"""
        try:
            key = self._get_encryption_key(uid)
            f = Fernet(key)
            api_key = f.decrypt(encrypted_key.encode()).decode()
            api_secret = f.decrypt(encrypted_secret.encode()).decode()
            return api_key, api_secret
        except (ValueError, TypeError, OSError) as e:
            log.error(f"Error decrypting API keys for UID {uid}: {e}")
            raise

    def save_api_keys(self, uid: str, api_key: str, api_secret: str):
        """Сохранить зашифрованные API ключи для UID"""
        try:
            encrypted_key, encrypted_secret = self.encrypt_api_keys(uid, api_key, api_secret)
            # Загружаем текущие данные пользователя (торги, настройки, возможно старые ключи)
            user_data = self.load_user_trades(uid)
            # Сохраняем зашифрованные ключи
            if "api_keys" not in user_data:
                user_data["api_keys"] = {}
            user_data["api_keys"]["encrypted_key"] = encrypted_key
            user_data["api_keys"]["encrypted_secret"] = encrypted_secret
            user_data["api_keys"]["saved_at"] = datetime.now().isoformat()
            user_data["last_updated"] = datetime.now().isoformat()
            # Сохраняем все данные пользователя (торги, настройки, ключи) в один файл
            file_path = self._get_uid_file(uid)
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(user_data, f, indent=2, default=str, ensure_ascii=False)
            log.info(f"API keys saved (encrypted) for UID {uid}")
        except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            log.error(f"Error saving API keys for UID {uid}: {e}")
            raise

    def load_api_keys(self, uid: str) -> Optional[Tuple[str, str]]:
        """Загрузить и расшифровать API ключи для UID"""
        try:
            user_data = self.load_user_trades(uid)
            if "api_keys" in user_data and "encrypted_key" in user_data["api_keys"] and "encrypted_secret" in user_data["api_keys"]:
                encrypted_key = user_data["api_keys"]["encrypted_key"]
                encrypted_secret = user_data["api_keys"]["encrypted_secret"]
                api_key, api_secret = self.decrypt_api_keys(uid, encrypted_key, encrypted_secret)
                log.info(f"API keys loaded (decrypted) for UID {uid}")
                return api_key, api_secret
        except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            log.error(f"Error loading API keys for UID {uid}: {e}")
        return None

    def has_api_keys(self, uid: str) -> bool:
        """Проверить, есть ли сохраненные API ключи для UID"""
        try:
            user_data = self.load_user_trades(uid)
            return (
                "api_keys" in user_data and "encrypted_key" in user_data.get("api_keys", {}) and "encrypted_secret" in user_data.get("api_keys", {})
            )
        except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            log.error(f"Error checking API keys for UID {uid}: {e}")
            return False

"""
Система управления рефералами (потокобезопасно)
"""

import json
import logging
import os
import threading
from datetime import datetime
from typing import Dict, Optional

log = logging.getLogger("referral")


class ReferralSystem:
    """Система управления рефералами"""

    def __init__(self, referrals_file: str = "referrals.json"):
        self.referrals_file = os.path.abspath(os.path.normpath(referrals_file))
        self.referrals: Dict[str, Dict] = {}
        _dir = os.path.dirname(self.referrals_file)
        self.pending_file = os.path.join(_dir, "pending_referrals.json")
        self.pending: Dict[str, Dict] = {}
        # RLock: один и тот же поток может брать lock несколько раз (add_pending_referral вызывает save_pending под lock)
        self._lock = threading.RLock()
        self.load_referrals()
        self.load_pending()

    def load_referrals(self):
        """Загрузить список рефералов из файла"""
        with self._lock:
            try:
                if os.path.exists(self.referrals_file):
                    with open(self.referrals_file, "r", encoding="utf-8") as f:
                        self.referrals = json.load(f)
                    log.info(f"Loaded {len(self.referrals)} referrals")
                else:
                    self.referrals = {}
            except (OSError, json.JSONDecodeError) as e:
                log.error(f"Error loading referrals: {e}")
                self.referrals = {}

    def save_referrals(self) -> bool:
        """Сохранить список рефералов в файл. Возвращает True при успехе."""
        with self._lock:
            try:
                with open(self.referrals_file, "w", encoding="utf-8") as f:
                    json.dump(self.referrals, f, indent=2, ensure_ascii=False)
                return True
            except OSError as e:
                log.error(f"Error saving referrals to %s: %s", self.referrals_file, e)
                return False

    def is_referral(self, uid: str) -> bool:
        """Проверить, является ли пользователь рефералом"""
        with self._lock:
            return uid in self.referrals

    def add_referral(self, uid: str, user_id: int, username: str = None) -> bool:
        """Добавить нового реферала"""
        with self._lock:
            if uid in self.referrals:
                return False
            self.referrals[uid] = {"user_id": user_id, "username": username, "registered_at": datetime.now().isoformat()}
            try:
                with open(self.referrals_file, "w", encoding="utf-8") as f:
                    json.dump(self.referrals, f, indent=2, ensure_ascii=False)
            except OSError as e:
                log.error(f"Error saving referrals: {e}")
                return False
            log.info(f"Added referral: {uid} (user_id: {user_id}, username: {username})")
            return True

    def get_referral(self, uid: str) -> Optional[Dict]:
        """Получить информацию о реферале"""
        with self._lock:
            return self.referrals.get(uid)

    def get_all_referrals(self) -> Dict[str, Dict]:
        """Получить всех рефералов"""
        with self._lock:
            return self.referrals.copy()

    def remove_referral(self, uid: str) -> bool:
        """Удалить реферала из памяти и из файла referrals.json."""
        with self._lock:
            if uid not in self.referrals:
                return False
            info = self.referrals.pop(uid)
            if not self.save_referrals():
                self.referrals[uid] = info
                log.error("Removed referral from memory but failed to save file; reverted.")
                return False
            log.info(f"Removed referral: {uid} (saved to %s)", self.referrals_file)
            return True

    def load_pending(self):
        """Загрузить список ожидающих одобрения"""
        with self._lock:
            try:
                if os.path.exists(self.pending_file):
                    with open(self.pending_file, "r", encoding="utf-8") as f:
                        self.pending = json.load(f)
                else:
                    self.pending = {}
            except (OSError, json.JSONDecodeError) as e:
                log.error(f"Error loading pending referrals: {e}")
                self.pending = {}

    def save_pending(self):
        """Сохранить список ожидающих"""
        with self._lock:
            try:
                with open(self.pending_file, "w", encoding="utf-8") as f:
                    json.dump(self.pending, f, indent=2, ensure_ascii=False)
            except OSError as e:
                log.error(f"Error saving pending referrals: {e}")

    def add_pending_referral(self, uid: str, user_id: int, username: str = None) -> bool:
        """Добавить реферала в ожидание одобрения"""
        with self._lock:
            if uid in self.referrals or uid in self.pending:
                return False
            self.pending[uid] = {"user_id": user_id, "username": username, "requested_at": datetime.now().isoformat()}
            self.save_pending()
            log.info(f"Added pending referral: {uid} (user_id: {user_id})")
            return True

    def get_pending_referrals(self) -> Dict[str, Dict]:
        """Получить всех ожидающих одобрения"""
        with self._lock:
            return self.pending.copy()

    def get_pending_referral(self, uid: str) -> Optional[Dict]:
        """Получить информацию об ожидающем реферале"""
        with self._lock:
            return self.pending.get(uid)

    def remove_pending_referral(self, uid: str) -> Optional[Dict]:
        """Удалить из ожидания (отклонение), вернуть инфо или None"""
        with self._lock:
            if uid not in self.pending:
                return None
            info = self.pending.pop(uid)
            self.save_pending()
            log.info(f"Removed pending referral: {uid}")
            return info

    def approve_pending_referral(self, uid: str) -> Optional[Dict]:
        """Одобрить реферала — перенести в подтверждённые, вернуть инфо"""
        with self._lock:
            if uid not in self.pending:
                return None
            info = self.pending.pop(uid)
            self.save_pending()
            user_id = info.get("user_id", 0)
            username = info.get("username")
            self.referrals[uid] = {"user_id": user_id, "username": username, "registered_at": datetime.now().isoformat()}
            try:
                with open(self.referrals_file, "w", encoding="utf-8") as f:
                    json.dump(self.referrals, f, indent=2, ensure_ascii=False)
            except OSError as e:
                log.error(f"Error saving referrals: {e}")
                return None
            log.info(f"Approved pending referral: {uid}")
            return {"user_id": user_id, "username": username}

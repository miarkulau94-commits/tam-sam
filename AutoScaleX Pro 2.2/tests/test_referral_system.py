"""
Unit tests for referral_system - ReferralSystem
"""

import os
import sys
import tempfile
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from referral_system import ReferralSystem


class TestReferralSystem:
    def test_add_referral(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            rs = ReferralSystem(referrals_file=path)
            ok = rs.add_referral("uid1", 12345, "user1")
            assert ok is True
            assert rs.is_referral("uid1") is True
            ref = rs.get_referral("uid1")
            assert ref["user_id"] == 12345
            assert ref["username"] == "user1"
        finally:
            os.remove(path)

    def test_add_referral_duplicate_returns_false(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            rs = ReferralSystem(referrals_file=path)
            rs.add_referral("uid2", 111, "a")
            ok = rs.add_referral("uid2", 222, "b")
            assert ok is False
        finally:
            os.remove(path)

    def test_is_referral_false(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            rs = ReferralSystem(referrals_file=path)
            assert rs.is_referral("unknown") is False
        finally:
            os.remove(path)

    def test_get_referral_nonexistent(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            rs = ReferralSystem(referrals_file=path)
            assert rs.get_referral("x") is None
        finally:
            os.remove(path)

    def test_remove_referral(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            rs = ReferralSystem(referrals_file=path)
            rs.add_referral("uid3", 999, "u")
            ok = rs.remove_referral("uid3")
            assert ok is True
            assert rs.is_referral("uid3") is False
        finally:
            os.remove(path)

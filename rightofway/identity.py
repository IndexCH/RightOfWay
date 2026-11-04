"""兼容旧的导入位置：认回同一个对象的规则在 rightofway/core/identity.py（核心的身份跟踪，接入代码用同一份）。"""
from .core.identity import match_identities, name_matches, strip_suffix, suffix_pattern, verify

__all__ = ["match_identities", "name_matches", "strip_suffix", "suffix_pattern", "verify"]

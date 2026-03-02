# -*- coding: utf-8 -*-
"""Audit helpers."""
import ipaddress
import logging

from flask import current_app, g, request
from flask_login import current_user

from core.extensions import db
from core.db_models import AuditLog
from core.guest import is_guest_user
from core.security import hash_identifier
from utils.parsers import json_or_none

logger = logging.getLogger(__name__)


_TRUSTED_PROXY_CACHE_KEY = 'trusted_proxy_cidrs_cache'
_TRUSTED_PROXY_CACHE_RAW_KEY = 'trusted_proxy_cidrs_raw'


def _parse_ip(value):
    if not value:
        return None
    try:
        return ipaddress.ip_address(str(value).strip())
    except (ValueError, TypeError):
        return None


def _tokenize_cidrs(raw):
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        parts = [str(item).strip() for item in raw if str(item).strip()]
    else:
        parts = [part.strip() for part in str(raw).replace(';', ',').split(',') if part.strip()]
    tokens = []
    for part in parts:
        tokens.extend([sub for sub in part.split() if sub])
    return tokens


def _trusted_proxy_networks():
    raw = current_app.config.get('TRUSTED_PROXY_CIDRS', '127.0.0.1/32,::1/128')
    cache_raw = current_app.extensions.get(_TRUSTED_PROXY_CACHE_RAW_KEY)
    cache_nets = current_app.extensions.get(_TRUSTED_PROXY_CACHE_KEY)
    if cache_raw == raw and isinstance(cache_nets, tuple):
        return cache_nets

    networks = []
    for token in _tokenize_cidrs(raw):
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            logger.warning("忽略无效 TRUSTED_PROXY_CIDRS 项: %s", token)

    result = tuple(networks)
    current_app.extensions[_TRUSTED_PROXY_CACHE_RAW_KEY] = raw
    current_app.extensions[_TRUSTED_PROXY_CACHE_KEY] = result
    return result


def _ip_in_trusted_networks(ip_obj, networks):
    return any(ip_obj in net for net in networks)


def _masked_ip_prefix(ip_obj):
    if ip_obj is None:
        return None
    if ip_obj.version == 4:
        octets = ip_obj.exploded.split('.')
        return f"{octets[0]}.{octets[1]}.{octets[2]}.0/24"
    hextets = ip_obj.exploded.split(':')
    return f"{hextets[0]}:{hextets[1]}:{hextets[2]}::/48"


def _get_client_ip_context():
    """提取客户端IP上下文：受信代理边界、来源、隐私化信息。"""
    remote_ip = _parse_ip(request.remote_addr)
    forwarded = request.headers.get('X-Forwarded-For', '')

    source = 'remote_addr'
    via_trusted_proxy = False
    client_ip = remote_ip

    if remote_ip and forwarded:
        networks = _trusted_proxy_networks()
        via_trusted_proxy = _ip_in_trusted_networks(remote_ip, networks)
        if via_trusted_proxy:
            forwarded_ips = []
            for token in forwarded.split(','):
                ip_obj = _parse_ip(token)
                if ip_obj is None:
                    logger.debug("X-Forwarded-For 中存在无效 IP，已忽略: %s", str(token)[:50])
                    continue
                forwarded_ips.append(ip_obj)
            if forwarded_ips:
                # 从右向左剥离受信代理，取最后一个非受信IP
                for candidate in reversed(forwarded_ips):
                    if not _ip_in_trusted_networks(candidate, networks):
                        client_ip = candidate
                        break
                else:
                    # 理论上很少出现：链路中均是受信代理，回退左端
                    client_ip = forwarded_ips[0]
                source = 'x_forwarded_for'

    client_ip_text = str(client_ip) if client_ip is not None else None
    return {
        'client_ip': client_ip_text,
        'ip_hash': hash_identifier(client_ip_text) if client_ip_text else None,
        'ip_prefix': _masked_ip_prefix(client_ip),
        'ip_source': source,
        'via_trusted_proxy': via_trusted_proxy,
    }


def _get_client_ip():
    """兼容旧调用：返回解析后的客户端 IP（不建议直接落库）。"""
    return _get_client_ip_context().get('client_ip')


def log_audit(action, resource_type=None, resource_id=None, metadata=None):
    """记录审计日志（受Feature Flag控制）"""
    if not current_app.config.get('FEATURE_AUDIT_LOGS'):
        return None
    try:
        actor_id = None
        actor_role = None
        if current_user.is_authenticated:
            actor_id = current_user.id if not is_guest_user(current_user) else None
            actor_role = getattr(current_user, 'role', None)
        ip_ctx = _get_client_ip_context()
        payload = metadata.copy() if isinstance(metadata, dict) else {}
        payload.setdefault('ip_source', ip_ctx.get('ip_source'))
        payload.setdefault('via_trusted_proxy', bool(ip_ctx.get('via_trusted_proxy')))
        if ip_ctx.get('ip_prefix'):
            payload.setdefault('ip_prefix', ip_ctx.get('ip_prefix'))
        entry = AuditLog(
            actor_id=actor_id,
            actor_role=actor_role,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            extra_data=json_or_none(payload),
            # 审计库仅保存哈希IP，避免持久化明文个人标识
            ip_address=ip_ctx.get('ip_hash'),
            user_agent=request.headers.get('User-Agent', '')[:200],
            request_id=getattr(g, 'request_id', None)
        )
        # 使用 savepoint 隔离，避免干扰调用方的主事务
        nested = db.session.begin_nested()
        try:
            db.session.add(entry)
            nested.commit()
        except Exception:
            nested.rollback()
            raise
        return entry
    except Exception as exc:
        logger.warning("审计日志写入失败: %s", exc)
        return None

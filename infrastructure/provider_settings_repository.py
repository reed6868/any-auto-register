from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session, select

from core.db import ProviderSettingModel, engine
from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository


_PROVIDER_KEY_ALIASES = {
    ("sms", "herosms"): "herosms_api",
    ("sms", "sms_activate"): "sms_activate_api",
    ("sms", "smsbower"): "smsbower_api",
}
_RUNTIME_PROVIDER_KEYS = {value: key for key, value in _PROVIDER_KEY_ALIASES.items()}


def canonical_provider_key(provider_type: str, provider_key: str) -> str:
    normalized_type = str(provider_type or "").strip()
    normalized_key = str(provider_key or "").strip()
    return _PROVIDER_KEY_ALIASES.get((normalized_type, normalized_key), normalized_key)


def runtime_provider_key(provider_type: str, provider_key: str) -> str:
    normalized_type = str(provider_type or "").strip()
    normalized_key = str(provider_key or "").strip()
    return _RUNTIME_PROVIDER_KEYS.get(normalized_key, (normalized_type, normalized_key))[1]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProviderSettingsRepository:
    def __init__(self, definitions: ProviderDefinitionsRepository | None = None):
        self.definitions = definitions or ProviderDefinitionsRepository()

    def list_by_type(self, provider_type: str) -> list[ProviderSettingModel]:
        with Session(engine) as session:
            return session.exec(
                select(ProviderSettingModel)
                .where(ProviderSettingModel.provider_type == provider_type)
                .order_by(ProviderSettingModel.id)
            ).all()

    def get(self, setting_id: int) -> ProviderSettingModel | None:
        with Session(engine) as session:
            return session.get(ProviderSettingModel, setting_id)

    def get_by_key(self, provider_type: str, provider_key: str) -> ProviderSettingModel | None:
        with Session(engine) as session:
            return session.exec(
                select(ProviderSettingModel)
                .where(ProviderSettingModel.provider_type == provider_type)
                .where(ProviderSettingModel.provider_key == provider_key)
            ).first()

    def resolve_runtime_settings(self, provider_type: str, provider_key: str, overrides: dict | None = None) -> dict:
        raw_key = str(provider_key or "").strip()
        canonical_key = canonical_provider_key(provider_type, raw_key)
        definition = self.definitions.get_by_key(provider_type, canonical_key)
        item = self.get_by_key(provider_type, raw_key)
        if item is None and canonical_key != raw_key:
            item = self.get_by_key(provider_type, canonical_key)
        payload: dict = {}
        if definition:
            for field in definition.get_fields():
                field_key = str(field.get("key") or "").strip()
                if not field_key:
                    continue
                default_value = field.get("default_value")
                if default_value not in (None, ""):
                    payload[field_key] = default_value
        if item:
            payload.update(item.get_config())
            payload.update(item.get_auth())
        payload.update(dict(overrides or {}))
        return payload

    def list_enabled(self, provider_type: str) -> list[ProviderSettingModel]:
        with Session(engine) as session:
            items = session.exec(
                select(ProviderSettingModel)
                .where(ProviderSettingModel.provider_type == provider_type)
                .where(ProviderSettingModel.enabled == True)  # noqa: E712
                .order_by(ProviderSettingModel.id)
            ).all()
        return sorted(items, key=lambda item: (not bool(item.is_default), int(item.id or 0)))

    def get_enabled_captcha_order(self) -> list[str]:
        configured = [
            item.provider_key
            for item in self.list_enabled("captcha")
            if item.provider_key not in {"", "manual", "local_solver"}
        ]
        merged: list[str] = []
        for key in configured:
            normalized = str(key or "").strip()
            if not normalized or normalized in {"manual", "local_solver"} or normalized in merged:
                continue
            merged.append(normalized)
        return merged

    def get_default_provider_key(self, provider_type: str, *, enabled_only: bool = True) -> str:
        items = self.list_enabled(provider_type) if enabled_only else self.list_by_type(provider_type)
        if not items:
            return ""
        return runtime_provider_key(provider_type, str(items[0].provider_key or ""))

    def delete(self, setting_id: int) -> bool:
        with Session(engine) as session:
            item = session.get(ProviderSettingModel, setting_id)
            if not item:
                return False
            provider_type = item.provider_type
            is_default = bool(item.is_default)
            session.delete(item)
            session.commit()

            remaining = session.exec(
                select(ProviderSettingModel)
                .where(ProviderSettingModel.provider_type == provider_type)
                .order_by(ProviderSettingModel.id)
            ).all()
            if is_default and remaining:
                fallback = remaining[0]
                fallback.is_default = True
                fallback.updated_at = _utcnow()
                session.add(fallback)
                session.commit()
                self._sync_legacy_config(provider_type, fallback)
            return True

    def save(
        self,
        *,
        setting_id: int | None,
        provider_type: str,
        provider_key: str,
        display_name: str,
        auth_mode: str,
        enabled: bool,
        is_default: bool,
        config: dict,
        auth: dict,
        metadata: dict,
    ) -> ProviderSettingModel:
        raw_key = str(provider_key or "").strip()
        provider_key = canonical_provider_key(provider_type, raw_key)
        definition = self.definitions.get_by_key(provider_type, provider_key)
        if not definition:
            raise ValueError(f"未知 provider: {provider_type}/{raw_key}")

        with Session(engine) as session:
            if setting_id:
                item = session.get(ProviderSettingModel, setting_id)
                if not item:
                    raise ValueError("provider setting 不存在")
            else:
                item = session.exec(
                    select(ProviderSettingModel)
                    .where(ProviderSettingModel.provider_type == provider_type)
                    .where(ProviderSettingModel.provider_key == provider_key)
                ).first()
                if item is None and raw_key != provider_key:
                    item = session.exec(
                        select(ProviderSettingModel)
                        .where(ProviderSettingModel.provider_type == provider_type)
                        .where(ProviderSettingModel.provider_key == raw_key)
                    ).first()
                if not item:
                    item = ProviderSettingModel(
                        provider_type=provider_type,
                        provider_key=provider_key,
                    )
                    item.created_at = _utcnow()

            item.provider_key = provider_key
            if is_default:
                for other in session.exec(
                    select(ProviderSettingModel).where(ProviderSettingModel.provider_type == provider_type)
                ).all():
                    if other.id != item.id and other.is_default:
                        other.is_default = False
                        other.updated_at = _utcnow()
                        session.add(other)

            item.display_name = display_name or definition.label or provider_key
            item.auth_mode = auth_mode or definition.default_auth_mode or ""
            item.enabled = bool(enabled)
            item.is_default = bool(is_default)
            item.set_config(config or {})
            item.set_auth(auth or {})
            item.set_metadata(metadata or {})
            item.updated_at = _utcnow()
            session.add(item)
            session.commit()
            session.refresh(item)

        return item

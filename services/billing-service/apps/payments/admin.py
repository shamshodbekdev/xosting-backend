import json

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.db.models import QuerySet
from django.utils.html import format_html, format_html_join

from .models import PaymentProvider, Transaction
from .registry import provider_classes

admin.site.site_header = "xosting.net billing"
admin.site.site_title = "xosting.net billing"
admin.site.index_title = "Billing administration"


def missing_config(key: str, config: dict) -> list[str]:
    cls = provider_classes().get(key)
    required = cls.required_config if cls else ()
    return [name for name in required if config.get(name) in ("", None)]


def _ensure_configured(form: forms.ModelForm, config: dict) -> None:
    if not (form.cleaned_data.get("is_active") and form.instance.pk):
        return
    if missing := missing_config(form.instance.key, config):
        raise forms.ValidationError(
            f"Cannot enable the provider: missing config keys {', '.join(missing)}."
        )


class ProviderToggleForm(forms.ModelForm):
    """Inline on/off switch on the changelist."""

    class Meta:
        model = PaymentProvider
        fields = ["is_active", "sort_order"]

    def clean(self):
        cleaned = super().clean()
        _ensure_configured(self, self.instance.config or {})
        return cleaned


class PaymentProviderForm(forms.ModelForm):
    config = forms.JSONField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 12, "cols": 80, "style": "font-family: monospace"}),
        help_text="Credentials and options as a JSON object (see the provider documentation).",
    )

    class Meta:
        model = PaymentProvider
        fields = ["name", "is_active", "sort_order", "config"]

    def clean_config(self):
        value = self.cleaned_data.get("config") or {}
        if not isinstance(value, dict):
            raise forms.ValidationError("Config must be a JSON object.")
        return value

    def clean(self):
        cleaned = super().clean()
        _ensure_configured(self, cleaned.get("config") or {})
        return cleaned


@admin.register(PaymentProvider)
class PaymentProviderAdmin(admin.ModelAdmin):
    form = PaymentProviderForm
    list_display = ["name", "key", "is_active", "configuration", "sort_order", "updated_at"]
    list_display_links = ["name"]
    list_editable = ["is_active", "sort_order"]
    list_filter = ["is_active"]
    readonly_fields = ["key", "webhook_url", "required_keys", "created_at", "updated_at"]
    fieldsets = [
        (None, {"fields": ["name", "key", "is_active", "sort_order"]}),
        ("Integration", {"fields": ["webhook_url", "required_keys", "config"]}),
        ("History", {"fields": ["created_at", "updated_at"]}),
    ]
    actions = ["enable_providers", "disable_providers"]

    def has_add_permission(self, request) -> bool:
        return False  # rows are created from the provider registry

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    @admin.display(description="Configured", boolean=True)
    def configuration(self, obj: PaymentProvider) -> bool:
        return not missing_config(obj.key, obj.config or {})

    @admin.display(description="Webhook URL")
    def webhook_url(self, obj: PaymentProvider) -> str:
        path = f"/api/v1/payments/webhook/{obj.key}/"
        if obj.key == "uzum":
            path += "<check|create|confirm|reverse|status>/"
        return f"{settings.PAYMENTS_PUBLIC_BASE_URL.rstrip('/')}{path}"

    @admin.display(description="Required config keys")
    def required_keys(self, obj: PaymentProvider) -> str:
        cls = provider_classes().get(obj.key)
        keys = cls.required_config if cls else ()
        return format_html_join(", ", "<code>{}</code>", ((k,) for k in keys)) or "-"

    def get_changelist_form(self, request, **kwargs):
        return ProviderToggleForm

    def _set_active(self, request, queryset: QuerySet, active: bool) -> None:
        changed, skipped = 0, []
        for record in queryset:
            if active and missing_config(record.key, record.config or {}):
                skipped.append(record.name)
                continue
            if record.is_active != active:
                record.is_active = active
                record.save(update_fields=["is_active", "updated_at"])
                changed += 1
        state = "enabled" if active else "disabled"
        self.message_user(request, f"{changed} provider(s) {state}.", messages.SUCCESS)
        if skipped:
            self.message_user(
                request,
                f"Not enabled (configuration incomplete): {', '.join(skipped)}.",
                messages.WARNING,
            )

    @admin.action(description="Enable selected providers")
    def enable_providers(self, request, queryset):
        self._set_active(request, queryset, True)

    @admin.action(description="Disable selected providers")
    def disable_providers(self, request, queryset):
        self._set_active(request, queryset, False)


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "provider",
        "order_id",
        "external_id",
        "amount",
        "status",
        "created_at",
        "performed_at",
    ]
    list_filter = ["provider", "status", "created_at"]
    search_fields = ["order_id", "external_id"]
    date_hierarchy = "created_at"
    list_select_related = ["provider"]
    readonly_fields = [
        field.name for field in Transaction._meta.fields if field.name != "raw_response"
    ] + ["raw_response_pretty"]
    exclude = ["raw_response"]

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    @admin.display(description="Raw provider data")
    def raw_response_pretty(self, obj: Transaction) -> str:
        return format_html(
            "<pre style='white-space: pre-wrap'>{}</pre>",
            json.dumps(obj.raw_response, indent=2, ensure_ascii=False),
        )

"""Compact configuration selector/input modal for /model, /tier, /budget, /attempts."""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from argus.config import ArgusConfig
from argus.providers.antigravity import is_antigravity_cli_available
from argus.providers.discovery import discover_all_models
from argus.providers.storage import ProviderStorage


# Design tokens matching styles.tcss and other modals
_TEXT = "#e1e4ec"
_SECONDARY = "#9298a8"
_MUTED = "#667085"
_BLUE = "#8188ff"
_GREEN = "#7bc79e"
_ORANGE = "#e88a13"
_RED = "#e07178"


class ConfigModal(ModalScreen[Optional[Any]]):
    """Contextual modal for setting individual runtime parameters."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        config_type: str,  # "model", "tier", "budget", "attempts"
        current_config: ArgusConfig,
    ) -> None:
        super().__init__()
        self.config_type = config_type
        self.current_config = current_config

    def _status_chip(self) -> str:
        if self.config_type == "model":
            if is_antigravity_cli_available():
                return f"[{_GREEN}]● host ready[/]"
            storage = ProviderStorage()
            active_p = storage.get_active_provider()
            if active_p and storage.get_api_key(active_p.name):
                return f"[{_GREEN}]● ready[/]"
            return f"[{_ORANGE}]● needs key[/]"
        elif self.config_type == "tier":
            return f"[{_BLUE}]● auto active[/]"
        elif self.config_type == "budget":
            return f"[{_SECONDARY}]USD cap[/]"
        else:
            return f"[{_SECONDARY}]turns[/]"

    def compose(self) -> ComposeResult:
        with Container(id="config-container", classes=f"config-mode-{self.config_type}"):
            if self.config_type == "model":
                with Horizontal(classes="modal-header"):
                    with Vertical(classes="modal-heading"):
                        yield Static("PROVIDER", classes="kicker")
                        yield Static("Inference model", id="config-title", classes="modal-title")
                    yield Static(self._status_chip(), id="config-status-chip", classes="modal-status")

                # Provider banner discovered off the UI thread in on_mount
                yield Static(
                    f"[{_MUTED}]● Detecting providers and available models…[/]",
                    id="config-provider-status",
                )

                with Horizontal(classes="field-head"):
                    yield Static("MODEL", classes="field-label")
                    yield Static(f"[{_MUTED}]available models[/]", id="config-models-meta", classes="field-meta")
                yield OptionList(id="config-options")

                yield Static("CUSTOM MODEL ID", classes="field-label")
                yield Input(
                    value=self.current_config.model or "",
                    id="config-input",
                    placeholder="e.g. gemini-3.8-flash-medium or gpt-4o-mini",
                )

                with Horizontal(classes="field-head"):
                    yield Static("PROVIDER API KEY", classes="field-label")
                    yield Static(f"[{_MUTED}]optional · OS keyring[/]", classes="field-meta")
                yield Input(
                    placeholder="sk-... (blank keeps current key)",
                    password=True,
                    id="config-api-key",
                )

                yield Static(
                    f"[{_MUTED}]Saved securely to the OS keyring. Host-native models need no key.[/]",
                    id="config-field-note",
                )

            elif self.config_type == "tier":
                with Horizontal(classes="modal-header"):
                    with Vertical(classes="modal-heading"):
                        yield Static("ROUTING", classes="kicker")
                        yield Static("Routing strategy", id="config-title", classes="modal-title")
                    yield Static(self._status_chip(), id="config-status-chip", classes="modal-status")

                yield Static("Select routing tier strategy:", id="config-prompt")
                from argus.providers.discovery import resolve_dynamic_tiers

                dyn = resolve_dynamic_tiers()
                t1 = dyn["t1"]["model"]
                t2 = dyn["t2"]["model"]
                t3 = dyn["t3"]["model"]
                # One line per option: the list is 6 rows tall, so wrapped labels
                # would push the last tier out of view.
                yield OptionList(
                    Option("● argus/auto      adaptive ladder (T1 → T2 → T3)", id="argus/auto"),
                    Option(f"○ argus/cheap     pinned · {t1}", id="argus/cheap"),
                    Option(f"○ argus/strong    pinned · {t2}", id="argus/strong"),
                    Option(f"○ argus/frontier  pinned · {t3}", id="argus/frontier"),
                    id="config-options",
                )

            elif self.config_type == "budget":
                with Horizontal(classes="modal-header"):
                    with Vertical(classes="modal-heading"):
                        yield Static("BUDGET", classes="kicker")
                        yield Static("Budget ceiling", id="config-title", classes="modal-title")
                    yield Static(self._status_chip(), id="config-status-chip", classes="modal-status")

                yield Static("Enter maximum USD spend limit ($0.01 – $5.00):", id="config-prompt")
                yield Input(value=str(self.current_config.budget_limit_usd), id="config-input")

            elif self.config_type == "attempts":
                with Horizontal(classes="modal-header"):
                    with Vertical(classes="modal-heading"):
                        yield Static("RECOVERY", classes="kicker")
                        yield Static("Recovery attempts", id="config-title", classes="modal-title")
                    yield Static(self._status_chip(), id="config-status-chip", classes="modal-status")

                yield Static("Enter maximum recovery attempts (1 – 10):", id="config-prompt")
                yield Input(value=str(self.current_config.max_attempts), id="config-input")

            yield Static("", id="config-error")

            with Horizontal(classes="modal-footer"):
                yield Static(f"[{_MUTED}]enter apply  ·  tab navigate[/]", classes="footer-hint")
                yield Button("Cancel (Esc)", id="btn-cancel")
                yield Button("Apply (Enter)", variant="primary", id="btn-apply")

    def on_mount(self) -> None:
        """Focus the input or options list, then load the model catalog in the background."""
        if self.query("#config-input"):
            self.query_one("#config-input", Input).focus()
        elif self.query("#config-options"):
            self.query_one("#config-options", OptionList).focus()

        if self.config_type == "model":
            self._load_model_catalog()

    @staticmethod
    def _describe_provider() -> str:
        """Build the provider status line (runs off the UI thread)."""
        storage = ProviderStorage()
        active_p = storage.get_active_provider()
        if active_p and active_p.name != "antigravity":
            key = storage.get_api_key(active_p.name)
            if key:
                return f"[bold {_BLUE}]● Active Provider:[/] {active_p.name.upper()} ({active_p.model}) [{_GREEN}]✓ Key Configured[/]"
            return f"[bold {_ORANGE}]● Active Provider:[/] {active_p.name.upper()} ({active_p.model}) [{_ORANGE}]○ Needs API Key[/]"

        if is_antigravity_cli_available():
            return f"[bold {_GREEN}]● Active Provider:[/] Antigravity Host Native ([{_SECONDARY}]Zero external API keys required[/])"

        return f"[bold {_ORANGE}]● Active Provider:[/] None configured ([{_SECONDARY}]Needs API Key or Ollama[/])"

    @work(thread=True, exclusive=True)
    def _load_model_catalog(self) -> None:
        """Discover providers/models without blocking the UI thread."""
        try:
            status = self._describe_provider()
            entries: List[Tuple[str, str, bool]] = [
                (m.model_id, m.display_line, bool(m.is_available)) for m in discover_all_models()
            ]
        except Exception as ex:  # provider probing must never crash the modal
            self.app.call_from_thread(
                self._apply_model_catalog,
                f"[bold {_ORANGE}]● Provider discovery failed:[/] {ex}",
                [],
            )
            return

        self.app.call_from_thread(self._apply_model_catalog, status, entries)

    def _apply_model_catalog(self, status: str, entries: List[Tuple[str, str, bool]]) -> None:
        """Populate the model list on the UI thread."""
        if not self.is_mounted:
            return
        try:
            self.query_one("#config-provider-status", Static).update(status)
            if self.query("#config-models-meta"):
                self.query_one("#config-models-meta", Static).update(f"[{_MUTED}]{len(entries)} available[/]")
            options = self.query_one("#config-options", OptionList)
            model_input = self.query_one("#config-input", Input)
        except NoMatches:
            return

        options.clear_options()
        for model_id, display_line, _ in entries:
            options.add_option(Option(Text.from_markup(display_line), id=model_id))

        if not model_input.value.strip() and entries:
            preferred = next((model_id for model_id, _, ok in entries if ok), entries[0][0])
            model_input.value = preferred

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """If user navigates options in the list, preview selection in the input box."""
        if event.option and self.query("#config-input"):
            self.query_one("#config-input", Input).value = str(event.option.id)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """If user selects an option in the list, update input or apply."""
        val = str(event.option.id)
        if self.query("#config-input"):
            self.query_one("#config-input", Input).value = val
        else:
            self.dismiss({"type": self.config_type, "value": val})

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press."""
        if event.button.id == "btn-cancel":
            self.dismiss(None)
        elif event.button.id == "btn-apply":
            self._apply_config()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key in input."""
        self._apply_config()

    def action_cancel(self) -> None:
        """Cancel via Escape key."""
        self.dismiss(None)

    def _apply_config(self) -> None:
        """Validate and apply the configuration value."""
        error_widget = self.query_one("#config-error", Static)

        if self.config_type == "model":
            val = self.query_one("#config-input", Input).value.strip()
            if not val:
                error_widget.update("Model name cannot be empty.")
                return

            target_provider = None
            if self.query("#config-api-key"):
                key_val = self.query_one("#config-api-key", Input).value.strip()
                if key_val:
                    # 1. Reject Groq keys with an informative explanation
                    if key_val.startswith("gsk_") or key_val.startswith("gsk-"):
                        error_widget.update(
                            "[bold red]Groq keys ('gsk_...') are strictly prohibited in Argus.[/bold red] "
                            "Groq is decommissioned in Argus. Supported: Host Native Antigravity (zero keys), OpenAI (sk-...), DeepSeek (sk-...), Google AI Studio (AIzaSy...), or OpenRouter (sk-or-...)."
                        )
                        return

                    # 2. Determine target provider from key signature or model name
                    storage = ProviderStorage()
                    models = discover_all_models(storage=storage)
                    model_by_id = {m.model_id: m for m in models}

                    if key_val.startswith("AIza"):
                        target_provider = "gemini"
                    elif key_val.startswith("gsk_") or key_val.startswith("gsk-"):
                        target_provider = "groq"
                    elif key_val.startswith("sk-or-") or key_val.startswith("sk-ant-"):
                        target_provider = "openrouter"
                    elif key_val.startswith("sk-proj-"):
                        target_provider = "openai"
                    elif "gemini" in val.lower():
                        target_provider = "gemini"
                    elif "deepseek" in val.lower() or "r1" in val.lower():
                        target_provider = "deepseek"
                    elif "llama" in val.lower() or "mixtral" in val.lower():
                        target_provider = "groq"
                    elif "gpt" in val.lower() or "o1" in val.lower() or "o3" in val.lower() or "o4" in val.lower():
                        target_provider = "openai"
                    elif val in model_by_id and model_by_id[val].provider != "antigravity":
                        target_provider = model_by_id[val].provider
                    else:
                        target_provider = "openai"

                    # 2. Save provider configuration & set active
                    try:
                        from argus.providers.models import PROVIDER_PRESETS, ProviderConfig
                        if target_provider in PROVIDER_PRESETS:
                            preset = PROVIDER_PRESETS[target_provider]
                            cfg = ProviderConfig(
                                name=target_provider,
                                base_url=preset.default_base_url,
                                model=val or preset.default_model,
                                api_key=key_val,
                            )
                            storage.save_provider(cfg)
                        else:
                            storage.set_api_key(target_provider, key_val)
                        storage.set_active_provider(target_provider)
                        discover_all_models(storage=storage, force_refresh=True)
                    except Exception as ex:
                        error_widget.update(f"Failed to save key: {ex}")
                        return
                else:
                    # When no key entered, check if an existing provider or host native should be active
                    storage = ProviderStorage()
                    models = discover_all_models(storage=storage)
                    model_by_id = {m.model_id: m for m in models}
                    if val in model_by_id:
                        prov = model_by_id[val].provider
                        if prov:
                            target_provider = prov
                            storage.set_active_provider(prov)
                    elif is_antigravity_cli_available() and ("gemini" in val.lower() or "claude" in val.lower() or "gpt-oss" in val.lower()):
                        target_provider = "antigravity"
                        storage.set_active_provider("antigravity")

            dismiss_data = {"type": "model", "value": val}
            if target_provider:
                dismiss_data["provider"] = target_provider
            self.dismiss(dismiss_data)

        elif self.config_type == "tier":
            opts = self.query_one("#config-options", OptionList)
            if opts.highlighted is not None:
                selected_opt = opts.get_option_at_index(opts.highlighted)
                self.dismiss({"type": "tier", "value": str(selected_opt.id)})
            else:
                self.dismiss({"type": "tier", "value": "argus/auto"})

        elif self.config_type == "budget":
            val_str = self.query_one("#config-input", Input).value.strip()
            try:
                val = float(val_str)
                if val <= 0.0 or val > 5.0:
                    error_widget.update("Budget must be between $0.01 and $5.00.")
                    return
                self.dismiss({"type": "budget", "value": val})
            except ValueError:
                error_widget.update("Please enter a valid numeric dollar amount.")

        elif self.config_type == "attempts":
            val_str = self.query_one("#config-input", Input).value.strip()
            try:
                val = int(val_str)
                if val < 1 or val > 10:
                    error_widget.update("Attempts must be between 1 and 10.")
                    return
                self.dismiss({"type": "attempts", "value": val})
            except ValueError:
                error_widget.update("Please enter a valid integer (1-10).")

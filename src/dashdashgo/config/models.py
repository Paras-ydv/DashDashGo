"""Typed report configuration.

One YAML file in `reports/` describes one pipeline end to end: where the report
lives, how to download it, how to clean it and where it lands in ClickHouse.
Validation is strict (unknown keys are rejected) and happens entirely at load
time, so a typo fails in milliseconds instead of after a 30 second browser run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Annotated, Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from dashdashgo.columns import ColumnType, Kind, parse_column_type
from dashdashgo.config.policy import blocked_launch_arg
from dashdashgo.ingestion.transforms import TransformStep, parse_transform_step
from dashdashgo.scheduling.cron import cron_trigger

IDENTIFIER = r"^[A-Za-z_][A-Za-z0-9_]*$"
Identifier = Annotated[str, Field(pattern=IDENTIFIER, max_length=64)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReportFormat(StrEnum):
    CSV = "csv"
    XLSX = "xlsx"
    JSON = "json"


# --- source (dashboard) -------------------------------------------------------


class Credentials(StrictModel):
    username: str = Field(min_length=1)
    password: SecretStr

    @field_validator("password")
    @classmethod
    def _password_not_empty(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("password must not be empty")
        return value


class MetabaseLocation(StrictModel):
    """Where the report lives, expressed the way a person would navigate to it."""

    collection: list[str] = Field(
        default_factory=list, description="Collection path from 'Our analytics'"
    )
    dashboard: str | None = None
    card: str | None = Field(default=None, description="Card title on the dashboard")
    question: str | None = None

    @field_validator("collection", mode="before")
    @classmethod
    def _single_collection(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value

    @model_validator(mode="after")
    def _exactly_one_target(self) -> Self:
        if bool(self.dashboard) == bool(self.question):
            raise ValueError("set exactly one of 'dashboard' or 'question'")
        if self.dashboard and not self.card:
            raise ValueError("'card' is required when downloading from a dashboard")
        if self.question and self.card:
            raise ValueError("'card' only applies to dashboards")
        return self

    @property
    def target_name(self) -> str:
        return self.dashboard or self.question or ""


class MetabaseSelectors(StrictModel):
    """UI hooks used by the Metabase adapter.

    Defaults match the pinned Metabase version (see docker-compose.yml) and
    prefer test ids, form names and accessible names over layout-dependent
    CSS. Override individual entries if a Metabase upgrade changes them.
    """

    username_input: str = 'input[name="username"]'
    password_input: str = 'input[name="password"]'
    submit_button: str = 'button[type="submit"]'
    login_error: str = 'form [role="alert"]'
    app_ready: str = '[aria-label="Navigation bar"]'
    dashcard: str = '[data-testid="dashcard"]'
    card_menu_button: str = "ellipsis icon"
    download_menu_item: str = "Download results"
    question_download_button: str = '[data-testid="question-results-download-button"]'
    download_dialog_heading: str = "Download data"
    download_button: str = "Download"
    formatted_checkbox: str = "Keep the data formatted"
    dismiss_buttons: list[str] = Field(
        default_factory=lambda: ["Start exploring", "Got it"],
        description="Buttons that close onboarding/announcement modals if they appear",
    )
    # Dashboard filter widgets (filter_mode: widget / auto)
    parameter_widget: str = '[data-testid="parameter-widget"]'
    parameter_widget_target: str = '[data-testid="parameter-value-widget-target"]'
    relative_date_option: str = "Relative date range…"
    list_search: str = "Search the list"
    apply_filter_button: str = r"^(Add|Update) filter$"
    clear_filter_button: str = "Clear"
    date_widget_icon: str = "calendar icon"
    relative_date_tab: str = "Previous"
    relative_date_interval: str = "Interval"
    relative_date_unit: str = "Unit"
    collection_title: str = "Add title"


FILENAME_DATE_TOKEN = "{date}"


class ExportOptions(StrictModel):
    format: ReportFormat
    formatted: bool = Field(
        default=False, description="Metabase 'Keep the data formatted' (locale-formatted values)"
    )
    filename: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9._{}-]+$",
        description="Name to store the raw download under, e.g. Weekly_Sales.csv; "
        "'{date}' is replaced by the run date. Default: the dashboard's own file name",
    )

    @model_validator(mode="after")
    def _filename_matches_format(self) -> Self:
        if self.filename is None:
            return self
        if re.search(r"[{}]", self.filename.replace(FILENAME_DATE_TOKEN, "")):
            raise ValueError(f"filename: only the {FILENAME_DATE_TOKEN} token is supported")
        if not self.filename.lower().endswith(f".{self.format.value}"):
            raise ValueError(f"filename must end with .{self.format.value} to match the format")
        return self

    def stored_filename(self, run_date: date, downloaded_name: str) -> str:
        if self.filename is None:
            return downloaded_name
        return self.filename.replace(FILENAME_DATE_TOKEN, run_date.isoformat())


class FilterSpec(StrictModel):
    """Long form of a filter: value(s) plus the label shown on the dashboard widget."""

    value: str | list[str]
    label: str | None = Field(
        default=None,
        description="Widget label; default: slug in title case (usage_date -> Usage Date)",
    )


@dataclass(frozen=True)
class FilterItem:
    slug: str
    values: list[str]
    label: str


class MetabaseSource(StrictModel):
    platform: Literal["metabase"] = "metabase"
    base_url: str = Field(pattern=r"^https?://[^\s/]+")
    login_path: str = "/auth/login"
    credentials: Credentials
    location: MetabaseLocation
    filters: dict[str, str | list[str] | FilterSpec] = Field(
        default_factory=dict,
        description="Parameter slug -> value(s), e.g. {usage_date: past7days, priority: [High]}",
    )
    filter_mode: Literal["auto", "widget", "url"] = Field(
        default="auto",
        description="widget: operate the dashboard's filter widgets like a user | url: set the "
        "parameters in the URL | auto: widgets, falling back to the URL if a widget can't be "
        "operated. The applied filters are verified in every mode.",
    )
    export: ExportOptions
    selectors: MetabaseSelectors = Field(default_factory=MetabaseSelectors)

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _widgets_need_a_dashboard(self) -> Self:
        if self.filter_mode == "widget" and self.filters and not self.location.dashboard:
            raise ValueError(
                "filter_mode 'widget' needs a dashboard; questions support 'url' or 'auto'"
            )
        return self

    def filter_items(self) -> list[FilterItem]:
        items = []
        for slug, spec in self.filters.items():
            raw, label = (spec.value, spec.label) if isinstance(spec, FilterSpec) else (spec, None)
            values = [raw] if isinstance(raw, str) else list(raw)
            items.append(FilterItem(slug, values, label or slug.replace("_", " ").title()))
        return items


# To support another dashboard tool, add its model here and turn this alias into
# an Annotated[Union[...], Field(discriminator="platform")].
SourceConfig = MetabaseSource


# --- browser ------------------------------------------------------------------


class Viewport(StrictModel):
    width: int = Field(default=1440, ge=320, le=7680)
    height: int = Field(default=900, ge=240, le=4320)


class BrowserConfig(StrictModel):
    engine: Literal["chromium", "firefox", "webkit"] = "chromium"
    headless: bool = True
    viewport: Viewport = Field(default_factory=Viewport)
    timeout_ms: int = Field(default=30_000, ge=1_000, le=300_000)
    navigation_timeout_ms: int = Field(default=45_000, ge=1_000, le=300_000)
    download_timeout_ms: int = Field(default=120_000, ge=1_000, le=1_800_000)
    screenshot_on_failure: bool = True
    trace: Literal["off", "on_failure", "always"] = "on_failure"
    launch_args: list[str] = Field(default_factory=list)

    @field_validator("launch_args")
    @classmethod
    def _safe_launch_args(cls, args: list[str]) -> list[str]:
        if blocked := sorted({flag for a in args if (flag := blocked_launch_arg(a))}):
            raise ValueError(
                f"browser flags not allowed: {', '.join(blocked)} "
                "(they could redirect the browser or expose it)"
            )
        return args


# --- ingestion ----------------------------------------------------------------


class ReaderOptions(StrictModel):
    encoding: str = "utf-8-sig"
    delimiter: str | None = Field(default=None, description="CSV delimiter; None = auto-detect")
    sheet: str | int | None = Field(default=None, description="XLSX sheet name/index; None = first")
    header_row: int = Field(default=0, ge=0)
    records_path: str | None = Field(
        default=None, description="JSON: dotted path to the list of records, e.g. 'data.items'"
    )


class QualityRule(StrictModel):
    column: str
    not_null: bool = False
    min: float | None = None
    max: float | None = None
    allowed: list[str] | None = None
    pattern: str | None = None

    @field_validator("pattern")
    @classmethod
    def _valid_regex(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid regex: {exc}") from exc
        return value

    @model_validator(mode="after")
    def _has_check(self) -> Self:
        if not (self.not_null or self.allowed or self.pattern) and (
            self.min is None and self.max is None
        ):
            raise ValueError(f"rule for '{self.column}' defines no check")
        return self


class QualityConfig(StrictModel):
    min_rows: int = Field(default=1, ge=0)
    on_invalid_rows: Literal["fail", "drop", "quarantine"] = Field(
        default="quarantine",
        description="fail: abort the run | drop: discard bad rows | quarantine: discard and "
        "store them in failures/ for inspection",
    )
    max_invalid_ratio: float = Field(
        default=0.05, ge=0, le=1, description="Abort if more than this share of rows is invalid"
    )
    rules: list[QualityRule] = Field(default_factory=list)


class IngestionConfig(StrictModel):
    reader: ReaderOptions = Field(default_factory=ReaderOptions)
    expected_columns: list[str] = Field(
        default_factory=list,
        description="Columns the downloaded file must contain (detects upstream schema drift)",
    )
    transforms: list[TransformStep] = Field(default_factory=list)
    quality: QualityConfig = Field(default_factory=QualityConfig)

    @field_validator("transforms", mode="before")
    @classmethod
    def _parse_transforms(cls, value: Any) -> Any:
        if not isinstance(value, list):
            raise ValueError("transforms must be a list")
        return [parse_transform_step(item) for item in value]


# --- destination --------------------------------------------------------------


class ColumnSpec(StrictModel):
    name: Identifier
    type: str
    format: str | None = Field(default=None, description="strptime format for date columns")
    comment: str | None = None

    @field_validator("name")
    @classmethod
    def _not_reserved(cls, value: str) -> str:
        if value.startswith("_"):
            raise ValueError("column names starting with '_' are reserved for lineage columns")
        return value

    @field_validator("type")
    @classmethod
    def _supported_type(cls, value: str) -> str:
        parse_column_type(value)
        return value

    @property
    def column_type(self) -> ColumnType:
        return parse_column_type(self.type)


class DestinationConfig(StrictModel):
    database: Identifier
    table: Identifier
    columns: list[ColumnSpec] = Field(min_length=1)
    order_by: list[str] = Field(min_length=1)
    partition_by: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_(),\s]+$")
    create_table: bool = True
    batch_size: int = Field(default=50_000, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        names = [c.name for c in self.columns]
        if duplicates := {n for n in names if names.count(n) > 1}:
            raise ValueError(f"duplicate columns: {sorted(duplicates)}")
        if unknown := [c for c in self.order_by if c not in names]:
            raise ValueError(f"order_by references unknown columns: {unknown}")
        nullable_keys = [
            c.name for c in self.columns if c.name in self.order_by and c.column_type.nullable
        ]
        if nullable_keys:
            raise ValueError(f"order_by columns must not be Nullable: {nullable_keys}")
        return self

    @property
    def qualified_table(self) -> str:
        return f"{self.database}.{self.table}"

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


# --- execution ----------------------------------------------------------------


class RetryConfig(StrictModel):
    max_attempts: int = Field(default=3, ge=1, le=10)
    initial_delay_seconds: float = Field(default=2.0, ge=0, le=300)
    backoff_multiplier: float = Field(default=2.0, ge=1, le=10)
    max_delay_seconds: float = Field(default=60.0, ge=0, le=3600)


class ScheduleConfig(StrictModel):
    enabled: bool = False
    cron: str | None = None
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _valid_cron(self) -> Self:
        if self.enabled and not self.cron:
            raise ValueError("'cron' is required when the schedule is enabled")
        if self.cron:
            try:
                cron_trigger(self.cron, self.timezone)
            except ValueError as exc:
                raise ValueError(f"invalid cron expression {self.cron!r}: {exc}") from exc
        return self


class ReportConfig(StrictModel):
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,62}$")]
    description: str = ""
    enabled: bool = True
    source: SourceConfig
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    destination: DestinationConfig
    retry: RetryConfig = Field(default_factory=RetryConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)

    @model_validator(mode="after")
    def _cross_section_checks(self) -> Self:
        columns = set(self.destination.column_names)
        quality = self.ingestion.quality
        if unknown := [r.column for r in quality.rules if r.column not in columns]:
            raise ValueError(f"quality rules reference unknown destination columns: {unknown}")
        kinds = {c.name: c.column_type.kind for c in self.destination.columns}
        numeric = {Kind.INTEGER, Kind.FLOAT, Kind.DECIMAL}
        if bad := [
            r.column
            for r in quality.rules
            if (r.min is not None or r.max is not None) and kinds[r.column] not in numeric
        ]:
            raise ValueError(f"min/max rules only apply to numeric columns: {bad}")
        return self

    @property
    def unique_key(self) -> list[str]:
        """Natural key of a row. It is deliberately the same as the ClickHouse ORDER BY:
        that is what makes ReplacingMergeTree collapse re-ingested rows (README: Idempotency)."""
        return self.destination.order_by

"""Автоматическое ревью кода (BidOps BO-13, требование «ревьюер берёт все
кодинговые задачи»).

Две половины одного контракта, обе живут в демоне runner'а, а не в агенте:

* **сторона кодера** — после успешного run задачи типа из ``reviewed_types``
  с опубликованной веткой демон заводит задачу ``code-review`` для ревьюера
  (``CONTROL_PLANE_AGENT_REVIEWER_PRINCIPAL``), связывает её ``spawned_by`` с
  исходной и назначает. Заводит демон, потому что именно он держит claim и
  знает, опубликована ли ветка; агент внутри run этого не знает и не должен
  распоряжаться чужой очередью;
* **сторона ревьюера** — по завершении задачи ``code-review`` демон читает
  вердикт из первой строки summary («ВЕРДИКТ: approved» /
  «ВЕРДИКТ: changes_requested») и переносит его в custom fields задачи
  (``verdict``, ``notes``), чтобы вердикт был машиночитаем, а не только
  текстом артефакта. Прежде это делал человек руками.

Режим ревьюера (``CONTROL_PLANE_AGENT_REVIEW_MODE``): ``agent`` — прежний путь,
ревьюер-агент со своим демоном; ``human`` — задача ``code-review`` назначается
человеку, и демон кодера сразу запрашивает на неё gate-approval. Решение по
approval и есть вердикт (approved / rejected = changes_requested): пока approval
не решён, задачу ревью нельзя закрыть, а решение видно в консоли и в
``cp_context`` человека. Второй агент-ревьюер в этом режиме не нужен.

Тип задачи может объявить работу после завершения сам (``completionSchema``,
CP-ADR-0061, амендмент 2026-09-25): тогда ядро заводит ревью при завершении
задачи, кем бы она ни была завершена, а демон сторону кодера пропускает —
без дублей. Этот модуль остаётся для типов без такой декларации, пока пакеты
не переведены.

Ничто здесь не роняет run: сбой при создании ревью или записи вердикта
пишется в лог и в комментарий, работа кодера остаётся завершённой.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_REVIEW_TYPE = "code-review"
DEFAULT_REVIEWED_TYPES = ("coding-task",)
VERDICTS = ("approved", "changes_requested")
REVIEW_MODES = ("agent", "human")
MAX_NOTES_CHARS = 4000
MAX_REQUIREMENTS_CHARS = 6000

_VERDICT_RE = re.compile(
    r"(?:ВЕРДИКТ|VERDICT)\s*[:：]\s*\**\s*(approved|changes[_ ]requested)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class ReviewPolicy:
    """Кому и за какие типы задач заводить ревью."""

    reviewer_principal_id: str
    reviewed_types: frozenset[str] = frozenset(DEFAULT_REVIEWED_TYPES)
    review_type: str = DEFAULT_REVIEW_TYPE
    base_branch: str = "main"
    mode: str = "agent"

    @property
    def human(self) -> bool:
        return self.mode == "human"

    def applies_to(self, task: Mapping[str, Any]) -> bool:
        return task.get("typeKey") in self.reviewed_types


def review_policy_from_env(env: Mapping[str, str] | None = None) -> ReviewPolicy | None:
    """``CONTROL_PLANE_AGENT_REVIEWER_PRINCIPAL`` включает авто-ревью; без него —
    поведение прежнее (ревью заводит человек)."""
    env = os.environ if env is None else env
    reviewer = (env.get("CONTROL_PLANE_AGENT_REVIEWER_PRINCIPAL") or "").strip()
    if not reviewer:
        return None
    types = tuple(
        t.strip()
        for t in (
            env.get("CONTROL_PLANE_AGENT_REVIEW_TASK_TYPES") or ",".join(DEFAULT_REVIEWED_TYPES)
        ).split(",")
        if t.strip()
    )
    mode = (env.get("CONTROL_PLANE_AGENT_REVIEW_MODE") or "agent").strip().lower()
    if mode not in REVIEW_MODES:
        raise ValueError(
            f"CONTROL_PLANE_AGENT_REVIEW_MODE={mode!r}: expected one of {', '.join(REVIEW_MODES)}"
        )
    return ReviewPolicy(
        mode=mode,
        reviewer_principal_id=reviewer,
        reviewed_types=frozenset(types or DEFAULT_REVIEWED_TYPES),
        review_type=(env.get("CONTROL_PLANE_AGENT_REVIEW_TYPE") or DEFAULT_REVIEW_TYPE).strip(),
        base_branch=(env.get("CONTROL_PLANE_AGENT_REVIEW_BASE") or "main").strip(),
    )


def published_commit(artifacts: Iterable[Any]) -> dict[str, Any] | None:
    """Metadata артефакта ``commit`` с ``published: true`` — то, что ревьюер
    сможет увидеть в forge. Неопубликованный коммит ревьюировать нечем."""
    for spec in artifacts:
        if getattr(spec, "type", None) != "commit":
            continue
        metadata = getattr(spec, "metadata", None) or {}
        if metadata.get("published") and metadata.get("branch") and metadata.get("commit"):
            return dict(metadata)
    return None


def build_review_task(
    task: Mapping[str, Any], commit: Mapping[str, Any], policy: ReviewPolicy
) -> dict[str, Any]:
    """Аргументы ``ControlPlaneClient.create_task`` для задачи ревью.

    Текст следует тому формату, в котором ревью заводил человек: что смотреть
    (ветка, коммит, база, команды), что требовалось (описание исходной задачи)
    и как оформить вердикт, чтобы демон его прочитал.
    """
    public_id = task["publicId"]
    branch = str(commit["branch"])
    sha = str(commit["commit"])
    # Where the work goes: a task cut from a feature branch is reviewed and
    # merged against that branch, not the runner's default.
    base = str(commit.get("targetBranch") or policy.base_branch)
    requirements = (task.get("description") or "").strip()
    if len(requirements) > MAX_REQUIREMENTS_CHARS:
        requirements = (
            requirements[:MAX_REQUIREMENTS_CHARS].rstrip()
            + "\n\n[… описание обрезано, полный текст — в задаче "
            + public_id
            + "]"
        )
    if policy.human:
        header = (
            "Автор — автономный агент (Claude Code), проверяет человек. Задача заведена демоном "
            "runner'а автоматически после успешного run с опубликованной веткой.\n\n"
            "РЕШЕНИЕ. На этой задаче стоит gate-approval «code review»: approve — изменения "
            "приняты, reject — нужны правки (в комментарии решения — что именно). Пока approval "
            "не решён, задачу нельзя закрыть. Если тип ревью объявляет исходы approval "
            "(code-review v3+, CP-ADR-0061), ядро само закроет задачу после решения, а при "
            "reject заведёт задачу на правки исполнителю; если тип так объявляет, approve "
            "ещё и вольёт ветку (поля задачи repository/branch/commit/targetBranch). Иначе "
            "закройте её вручную.\n\n"
        )
    else:
        header = (
            "Автор — автономный агент (Claude Code), проверяет другой вендор. Задача заведена "
            "демоном runner'а автоматически после успешного run с опубликованной веткой.\n\n"
            "ФОРМАТ РЕЗУЛЬТАТА. Первой строкой summary напиши ровно «ВЕРДИКТ: approved» или "
            "«ВЕРДИКТ: changes_requested», дальше — разбор: что проверено, найденные "
            "несоответствия с путями и строками, что блокирует, что можно отложить. Демон "
            "переносит вердикт и разбор в поля задачи (verdict, notes); если доступен Control "
            "Plane MCP, продублируй их через cp_update_task.\n\n"
        )
    description = (
        f"Ревью работы по {public_id}: {task.get('title', '').strip()}\n\n"
        + header
        + "ЧТО СМОТРЕТЬ. Рабочая копия — текущий каталог. Ветка "
        f"{branch}, коммит {sha}, целевая ветка origin/{base}. Начни с `git fetch origin`, "
        f"затем смотри ИЗМЕНЕНИЯ ВЕТКИ, а не расстояние до текущего origin/{base}: "
        f"`git diff $(git merge-base origin/{base} origin/{branch}) origin/{branch}`. Ветка "
        f"могла быть отведена от более старого {base}, и в `git diff origin/{base} …` чужие "
        "свежие коммиты выглядят как «удаления» — это не часть изменения и не блокер; при "
        f"необходимости проверь `git merge --no-commit origin/{branch}` поверх origin/{base} на "
        "конфликты. Прогони тесты проекта в рабочей копии. Соседние каталоги (path-зависимости) "
        "уже стоят на закреплённых ревизиях.\n\n"
        "КРИТЕРИИ. (1) Сделано ли то, что требовалось — по пунктам ниже; (2) не сломано ли "
        "существующее: тесты, миграции, контракты API; (3) нет ли секретов, локальных путей и "
        "лишних файлов в коммите; (4) соответствие архитектурным границам проекта (ADR). "
        "Несоответствие требованиям или красные тесты — changes_requested.\n\n"
        f"ЧТО ТРЕБОВАЛОСЬ (из {public_id}):\n{requirements or '(описание отсутствует)'}"
    )
    # Only the key, never a version: the server resolves it to the newest
    # active version, which is how reviews pick up a type that declares
    # approval outcomes (code-review v3, CP-ADR-0061) without a redeploy.
    spec: dict[str, Any] = {
        "title": f"Ревью {public_id}: {task.get('title', '').strip()}"[:500],
        "description": description,
        "priority": task.get("priority") or "medium",
        "type_key": policy.review_type,
        "assignee_id": policy.reviewer_principal_id,
    }
    if task.get("workspaceId"):
        spec["workspace_id"] = str(task["workspaceId"])
    fields = review_fields(commit, policy)
    if fields:
        spec["custom_fields"] = fields
    return spec


def review_fields(commit: Mapping[str, Any], policy: ReviewPolicy) -> dict[str, str]:
    """Custom fields of the review: what exactly was reviewed and where it goes.

    Machine-readable on the review itself, so the review type's approval
    outcome can hand them to a skill (``$.task.customFields.<key>``,
    CP-ADR-0061) instead of parsing the description. ``repository`` is only
    there when the branch was published to a remote that can be shared.
    """
    fields = {
        "repository": commit.get("repository"),
        "branch": commit.get("branch"),
        "commit": commit.get("commit"),
        "targetBranch": commit.get("targetBranch") or policy.base_branch,
    }
    return {key: str(value) for key, value in fields.items() if value}


def parse_verdict(summary: str | None) -> tuple[str | None, str]:
    """(verdict, notes) из summary ревьюера. Вердикт ищется по всему тексту,
    но ожидается первой строкой; notes — summary без строки вердикта."""
    text = (summary or "").strip()
    if not text:
        return None, ""
    match = _VERDICT_RE.search(text)
    if match is None:
        return None, text[:MAX_NOTES_CHARS]
    verdict = match.group(1).lower().replace(" ", "_")
    lines = text.splitlines()
    notes_lines = [line for line in lines if not _VERDICT_RE.search(line)]
    notes = "\n".join(notes_lines).strip()
    return verdict, notes[:MAX_NOTES_CHARS]


def summary_of(artifacts: Iterable[Any]) -> str | None:
    """Текст отчёта агента из артефакта ``report`` (Claude Code и Codex пишут
    ``content.summary``)."""
    for spec in artifacts:
        if getattr(spec, "type", None) != "report":
            continue
        content = getattr(spec, "content", None) or {}
        summary = content.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary
    return None

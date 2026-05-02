"""
AutoResearcher Two-Tier Memory System

Maintains a constant-size memory regardless of how long the agent runs:
- Tier 1 (PROJECT_BRIEF.md): Frozen reference, never modified by the agent
- Tier 2 (MEMORY_LOG.md): Rolling log with auto-compaction

Total memory budget: ~5000 chars (~1500 tokens) — always.
"""

import time
from pathlib import Path
from typing import Optional


class MemoryManager:
    """Two-tier memory with automatic compaction.

    The key insight: long-running agents accumulate context that grows
    without bound, leading to degraded performance and ballooning costs.
    This system caps memory at a fixed budget by:
    - Keeping milestones (key results) in a priority queue, oldest dropped first
    - Keeping only the N most recent decisions
    - Never modifying the frozen project brief
    - Maintaining per-cycle exploration summaries for code agent context accumulation
    """

    def __init__(
        self,
        project_dir: Path,
        workspace_dir: Path,
        brief_max: int = 3000,
        log_max: int = 2000,
        milestone_max: int = 1200,
        max_recent: int = 15,
        max_summaries: int = 5,
    ):
        self.project_dir = Path(project_dir)
        self.workspace_dir = Path(workspace_dir)
        self.brief_path = self.project_dir / "PROJECT_BRIEF.md"
        self.log_path = self.workspace_dir / "MEMORY_LOG.md"
        self.summaries_dir = self.workspace_dir / "code_agent_summaries"
        self.current_summary_path = self.workspace_dir / "CURRENT_CYCLE_SUMMARY.md"
        self.brief_max = brief_max
        self.log_max = log_max
        self.milestone_max = milestone_max
        self.max_recent = max_recent
        self.max_summaries = max_summaries

        # Ensure directories and files exist
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.summaries_dir.mkdir(parents=True, exist_ok=True)
        if not self.log_path.exists():
            self._init_log()
        if not self.current_summary_path.exists():
            self.current_summary_path.write_text("# Current Cycle Summary\n\nNo summary yet.\n")

    def get_brief(self) -> str:
        """Return the frozen project brief (Tier 1)."""
        if self.brief_path.exists():
            content = self.brief_path.read_text()
            return content[: self.brief_max]
        return ""

    def get_log(self) -> str:
        """Return the rolling memory log (Tier 2)."""
        if self.log_path.exists():
            return self.log_path.read_text()
        return ""

    def get_full_context(self) -> str:
        """Return combined memory for agent consumption."""
        brief = self.get_brief()
        log = self.get_log()
        return f"## Project Brief\n{brief}\n\n## Memory Log\n{log}"

    def get_code_agent_context(self, cycle_count: int) -> str:
        """Return accumulated exploration summaries for code agent context.
        
        This includes:
        1. Current cycle summary (if exists)
        2. Recent N cycle summaries (for context accumulation)
        
        Args:
            cycle_count: Current cycle number
            
        Returns:
            Markdown-formatted context string with exploration history
        """
        sections = ["## Code Agent Exploration History\n"]
        
        # Add current cycle summary if it exists and is different from archived ones
        if self.current_summary_path.exists():
            content = self.current_summary_path.read_text()
            if "No summary yet" not in content:
                sections.append(f"### Current Cycle (Cycle {cycle_count})\n{content}\n")
        
        # Add recent historical summaries
        if self.summaries_dir.exists():
            summary_files = sorted(
                self.summaries_dir.glob("cycle_*.md"),
                key=lambda p: int(p.stem.split("_")[1]) if p.stem.split("_")[1].isdigit() else 0,
                reverse=True
            )[:self.max_summaries]
            
            for summary_file in reversed(summary_files):  # Oldest first
                try:
                    cycle_num = int(summary_file.stem.split("_")[1])
                    content = summary_file.read_text()
                    sections.append(f"### Cycle {cycle_num}\n{content}\n")
                except (ValueError, IndexError):
                    continue
        
        return "\n".join(sections) if len(sections) > 1 else "## Code Agent Exploration History\n\nNo prior exploration history.\n"

    def save_cycle_summary(self, summary: str, cycle_count: int):
        """Save a cycle summary and archive it.
        
        Args:
            summary: The markdown-formatted summary from the summarizer agent
            cycle_count: Current cycle number
        """
        # Save to current summary path
        self.current_summary_path.write_text(summary)
        
        # Archive to summaries directory
        archive_path = self.summaries_dir / f"cycle_{cycle_count}.md"
        archive_path.write_text(summary)
        
        # Clean up old summaries if over budget
        self._compact_summaries()

    def _compact_summaries(self):
        """Keep only the most recent N summaries."""
        if not self.summaries_dir.exists():
            return
            
        summary_files = sorted(
            self.summaries_dir.glob("cycle_*.md"),
            key=lambda p: int(p.stem.split("_")[1]) if p.stem.split("_")[1].isdigit() else 0
        )
        
        # Remove oldest files if we have too many
        while len(summary_files) > self.max_summaries:
            oldest = summary_files.pop(0)
            oldest.unlink()

    def clear_current_summary(self):
        """Clear the current cycle summary at the start of a new cycle."""
        self.current_summary_path.write_text("# Current Cycle Summary\n\nNo summary yet.\n")

    def log_milestone(self, entry: str):
        """Add a key result milestone. Auto-compacts if over budget."""
        sections = self._parse_log()
        timestamp = time.strftime("%m-%d %H:%M")
        sections["milestones"].append(f"[{timestamp}] {entry}")

        # Compact: drop oldest milestones if over char budget
        while self._section_size(sections["milestones"]) > self.milestone_max and len(sections["milestones"]) > 1:
            sections["milestones"].pop(0)

        self._write_log(sections)

    def log_decision(self, entry: str):
        """Add a recent decision. Auto-compacts to keep only last N."""
        sections = self._parse_log()
        timestamp = time.strftime("%m-%d %H:%M")
        sections["decisions"].append(f"[{timestamp}] {entry}")

        # Compact: keep only last N entries
        if len(sections["decisions"]) > self.max_recent:
            sections["decisions"] = sections["decisions"][-self.max_recent :]

        self._write_log(sections)

    def _init_log(self):
        """Create initial empty memory log."""
        content = "# Memory Log\n\n## Key Results\n\n## Recent Decisions\n"
        self.log_path.write_text(content)

    def _parse_log(self) -> dict:
        """Parse MEMORY_LOG.md into sections."""
        content = self.get_log()
        sections = {"milestones": [], "decisions": []}

        current_section = None
        for line in content.split("\n"):
            line_stripped = line.strip()
            if line_stripped == "## Key Results":
                current_section = "milestones"
            elif line_stripped == "## Recent Decisions":
                current_section = "decisions"
            elif line_stripped.startswith("[") and current_section:
                sections[current_section].append(line_stripped)

        return sections

    def _write_log(self, sections: dict):
        """Write sections back to MEMORY_LOG.md."""
        lines = ["# Memory Log", "", "## Key Results"]
        for entry in sections["milestones"]:
            lines.append(entry)
        lines.append("")
        lines.append("## Recent Decisions")
        for entry in sections["decisions"]:
            lines.append(entry)
        lines.append("")

        content = "\n".join(lines)

        # Final safety check: total log must fit budget
        if len(content) > self.log_max:
            # Aggressive compaction: trim milestones first, then decisions
            while len(content) > self.log_max and len(sections["milestones"]) > 1:
                sections["milestones"].pop(0)
                content = self._build_content(sections)
            while len(content) > self.log_max and len(sections["decisions"]) > 1:
                sections["decisions"].pop(0)
                content = self._build_content(sections)

        self.log_path.write_text(content)

    def _build_content(self, sections: dict) -> str:
        lines = ["# Memory Log", "", "## Key Results"]
        lines.extend(sections["milestones"])
        lines.append("")
        lines.append("## Recent Decisions")
        lines.extend(sections["decisions"])
        lines.append("")
        return "\n".join(lines)

    def _section_size(self, entries: list) -> int:
        return sum(len(e) for e in entries)

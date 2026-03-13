#!/usr/bin/env python3
"""
Claude Chat Cleaner — Terminal UI for managing Claude Code conversation history.

Navigate with arrow keys, select with Space, delete with 'd', and quit with 'q'.
"""

import curses
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def human_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def dir_size(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
    return total


def get_chat_preview(jsonl_path: Path) -> Tuple[str, Optional[str], int]:
    """Extract first user message, timestamp, and line count from a JSONL chat file."""
    first_user_msg = None
    timestamp = None
    line_count = 0

    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line_count += 1
                if first_user_msg and timestamp:
                    continue  # just counting lines
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if not timestamp and entry.get("timestamp"):
                    timestamp = entry["timestamp"]

                if not first_user_msg and entry.get("type") == "user" and not entry.get("isMeta"):
                    msg = entry.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        # Strip XML tags for cleaner preview
                        clean = content
                        while "<" in clean and ">" in clean:
                            start = clean.find("<")
                            end = clean.find(">", start)
                            if end == -1:
                                break
                            clean = clean[:start] + clean[end + 1:]
                        clean = clean.strip()
                        if clean:
                            first_user_msg = clean[:120]
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text", "").strip()
                                if text:
                                    first_user_msg = text[:120]
                                    break
    except Exception:
        pass

    if not first_user_msg:
        first_user_msg = "(no preview available)"

    return first_user_msg, timestamp, line_count


def format_timestamp(ts: Optional[str]) -> str:
    if not ts:
        return "unknown date"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts[:16] if len(ts) >= 16 else ts


def friendly_project_name(dirname: str) -> str:
    home = str(Path.home())
    home_prefix = home.replace("/", "-").lstrip("-")
    if dirname.startswith("-"):
        dirname = dirname[1:]
    if dirname.startswith(home_prefix):
        dirname = dirname[len(home_prefix):]
    path = dirname.replace("-", "/")
    if not path:
        path = "/"
    return "~" + path


class ChatInfo:
    def __init__(self, path: Path):
        self.path = path
        self.name = path.stem
        self.size = path.stat().st_size
        self.preview: Optional[str] = None
        self.timestamp: Optional[str] = None
        self.line_count: int = 0
        self.selected: bool = False
        self._loaded = False

    def load_preview(self):
        if not self._loaded:
            self.preview, self.timestamp, self.line_count = get_chat_preview(self.path)
            self._loaded = True

    @property
    def companion_dir(self) -> Optional[Path]:
        d = self.path.parent / self.name
        return d if d.is_dir() else None


class ProjectInfo:
    def __init__(self, path: Path):
        self.path = path
        self.dirname = path.name
        self.friendly_name = friendly_project_name(path.name)
        self.chats: List[ChatInfo] = []
        self.size = 0
        self._scan()

    def _scan(self):
        self.chats = []
        self.size = 0
        if not self.path.is_dir():
            return
        for f in sorted(self.path.iterdir()):
            if f.suffix == ".jsonl" and f.is_file():
                chat = ChatInfo(f)
                self.chats.append(chat)
                self.size += chat.size
                comp = chat.companion_dir
                if comp:
                    self.size += dir_size(comp)
        # Sort by modification time, newest first
        self.chats.sort(key=lambda c: c.path.stat().st_mtime, reverse=True)

    @property
    def has_memory(self) -> bool:
        return (self.path / "memory").is_dir() or (self.path / "MEMORY.md").exists()


# ─── Views ────────────────────────────────────────────────────────────────────


class App:
    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.projects: List[ProjectInfo] = []
        self.view = "projects"  # "projects" or "chats"
        self.cursor = 0
        self.scroll_offset = 0
        self.current_project: Optional[ProjectInfo] = None
        self.status_msg = ""
        self.confirm_action = None  # (message, callback)

        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)
        curses.init_pair(4, curses.COLOR_YELLOW, -1)
        curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(6, curses.COLOR_WHITE, curses.COLOR_RED)
        curses.init_pair(7, curses.COLOR_MAGENTA, -1)
        curses.curs_set(0)

        self._load_projects()

    def _load_projects(self):
        self.projects = []
        if not CLAUDE_PROJECTS_DIR.is_dir():
            return
        for d in sorted(CLAUDE_PROJECTS_DIR.iterdir()):
            if d.is_dir():
                proj = ProjectInfo(d)
                if proj.chats:
                    self.projects.append(proj)
        self.projects.sort(key=lambda p: p.size, reverse=True)

    @property
    def total_size(self) -> int:
        return sum(p.size for p in self.projects)

    def run(self):
        while True:
            self.draw()
            key = self.stdscr.getch()

            if self.confirm_action:
                self._handle_confirm(key)
                continue

            if key == ord("q"):
                if self.view == "chats":
                    self.view = "projects"
                    self.cursor = 0
                    self.scroll_offset = 0
                    self.status_msg = ""
                    self._load_projects()
                else:
                    break
            elif key == curses.KEY_UP or key == ord("k"):
                self._move_cursor(-1)
            elif key == curses.KEY_DOWN or key == ord("j"):
                self._move_cursor(1)
            elif key == ord("\n") or key == curses.KEY_RIGHT:
                self._enter()
            elif key == curses.KEY_LEFT:
                if self.view == "chats":
                    self.view = "projects"
                    self.cursor = 0
                    self.scroll_offset = 0
                    self.status_msg = ""
                    self._load_projects()
            elif key == ord(" "):
                self._toggle_select()
            elif key == ord("d"):
                self._delete()
            elif key == ord("a"):
                self._select_all()
            elif key == ord("D"):
                self._delete_project()

    def _move_cursor(self, delta: int):
        items = self._current_items()
        if not items:
            return
        self.cursor = max(0, min(len(items) - 1, self.cursor + delta))
        self._adjust_scroll()

    def _adjust_scroll(self):
        max_h, _ = self.stdscr.getmaxyx()
        visible = max_h - 6  # header + footer
        if self.cursor < self.scroll_offset:
            self.scroll_offset = self.cursor
        elif self.cursor >= self.scroll_offset + visible:
            self.scroll_offset = self.cursor - visible + 1

    def _clamp_scroll(self):
        """Ensure scroll_offset is valid after items are removed."""
        items = self._current_items()
        max_h, _ = self.stdscr.getmaxyx()
        visible = max_h - 6
        max_offset = max(0, len(items) - visible)
        self.scroll_offset = min(self.scroll_offset, max_offset)
        self._adjust_scroll()

    def _current_items(self):
        if self.view == "projects":
            return self.projects
        return self.current_project.chats if self.current_project else []

    def _enter(self):
        if self.view == "projects" and self.projects:
            self.current_project = self.projects[self.cursor]
            # Lazy-load previews
            for chat in self.current_project.chats:
                chat.load_preview()
            self.view = "chats"
            self.cursor = 0
            self.scroll_offset = 0
            self.status_msg = ""

    def _toggle_select(self):
        if self.view != "chats" or not self.current_project:
            return
        chats = self.current_project.chats
        if chats:
            chats[self.cursor].selected = not chats[self.cursor].selected

    def _select_all(self):
        if self.view != "chats" or not self.current_project:
            return
        all_selected = all(c.selected for c in self.current_project.chats)
        for c in self.current_project.chats:
            c.selected = not all_selected

    def _delete(self):
        if self.view != "chats" or not self.current_project:
            return
        selected = [c for c in self.current_project.chats if c.selected]
        if not selected:
            # Delete the one under cursor
            if self.current_project.chats:
                chat = self.current_project.chats[self.cursor]
                size = human_size(chat.size)
                self.confirm_action = (
                    f"Delete this chat ({size})? [y/n]",
                    lambda: self._do_delete([chat]),
                )
        else:
            total = sum(c.size for c in selected)
            self.confirm_action = (
                f"Delete {len(selected)} chats ({human_size(total)})? [y/n]",
                lambda: self._do_delete(selected),
            )

    def _delete_project(self):
        if self.view == "projects" and self.projects:
            proj = self.projects[self.cursor]
            self.confirm_action = (
                f"Delete ALL chats in {proj.friendly_name} ({human_size(proj.size)})? [y/n]",
                lambda: self._do_delete_project(proj),
            )
        elif self.view == "chats" and self.current_project:
            proj = self.current_project
            self.confirm_action = (
                f"Delete ALL chats in {proj.friendly_name} ({human_size(proj.size)})? [y/n]",
                lambda: self._do_delete_project(proj),
            )

    def _do_delete(self, chats: List[ChatInfo]):
        count = 0
        freed = 0
        for chat in chats:
            try:
                freed += chat.size
                chat.path.unlink()
                comp = chat.companion_dir
                if comp:
                    freed += dir_size(comp)
                    shutil.rmtree(comp)
                count += 1
            except Exception as e:
                self.status_msg = f"Error: {e}"
                return
        self.current_project._scan()
        for chat in self.current_project.chats:
            chat.load_preview()
        self.cursor = min(self.cursor, max(0, len(self.current_project.chats) - 1))
        self._clamp_scroll()
        self.status_msg = f"Deleted {count} chat(s), freed {human_size(freed)}"

    def _do_delete_project(self, proj: ProjectInfo):
        freed = 0
        count = 0
        for chat in list(proj.chats):
            try:
                freed += chat.size
                chat.path.unlink()
                comp = chat.companion_dir
                if comp:
                    freed += dir_size(comp)
                    shutil.rmtree(comp)
                count += 1
            except Exception:
                pass
        proj._scan()
        self._load_projects()
        self.cursor = min(self.cursor, max(0, len(self._current_items()) - 1))
        self._clamp_scroll()
        if self.view == "chats":
            if not self.current_project.chats:
                self.view = "projects"
                self.cursor = 0
                self.scroll_offset = 0
        self.status_msg = f"Deleted {count} chat(s), freed {human_size(freed)}"

    def _handle_confirm(self, key):
        if key == ord("y") or key == ord("Y"):
            _, callback = self.confirm_action
            self.confirm_action = None
            callback()
        elif key == ord("n") or key == ord("N") or key == 27:  # ESC
            self.confirm_action = None
            self.status_msg = "Cancelled."

    # ─── Drawing ──────────────────────────────────────────────────────────

    def draw(self):
        self.stdscr.erase()
        max_h, max_w = self.stdscr.getmaxyx()

        if max_h < 5 or max_w < 40:
            self.stdscr.addstr(0, 0, "Terminal too small")
            self.stdscr.refresh()
            return

        # Header
        title = " Claude Chat Cleaner "
        total = f" Total: {human_size(self.total_size)} "
        self.stdscr.attron(curses.A_BOLD)
        self.stdscr.addnstr(0, 0, title, max_w - 1, curses.color_pair(1) | curses.A_BOLD)
        if len(title) + len(total) < max_w:
            self.stdscr.addnstr(0, max_w - len(total) - 1, total, max_w - 1, curses.color_pair(4))
        self.stdscr.attroff(curses.A_BOLD)

        # Breadcrumb
        if self.view == "projects":
            crumb = "Projects"
        else:
            crumb = f"Projects > {self.current_project.friendly_name}"
        self.stdscr.addnstr(1, 0, crumb, max_w - 1, curses.color_pair(7))
        self.stdscr.addnstr(2, 0, "─" * (max_w - 1), max_w - 1, curses.color_pair(1))

        # Items
        visible_h = max_h - 6
        items = self._current_items()

        for i in range(visible_h):
            idx = i + self.scroll_offset
            y = i + 3
            if y >= max_h - 2:
                break
            if idx >= len(items):
                break

            is_cursor = idx == self.cursor
            item = items[idx]

            if self.view == "projects":
                self._draw_project_row(y, max_w, item, is_cursor)
            else:
                self._draw_chat_row(y, max_w, item, is_cursor)

        # Confirm bar
        if self.confirm_action:
            msg, _ = self.confirm_action
            self.stdscr.addnstr(max_h - 3, 0, " " * (max_w - 1), max_w - 1, curses.color_pair(6))
            self.stdscr.addnstr(max_h - 3, 1, msg, max_w - 2, curses.color_pair(6) | curses.A_BOLD)

        # Status
        if self.status_msg:
            self.stdscr.addnstr(max_h - 2, 0, self.status_msg, max_w - 1, curses.color_pair(2))

        # Footer
        footer_line = max_h - 1
        self.stdscr.addnstr(footer_line, 0, " " * (max_w - 1), max_w - 1, curses.color_pair(5))
        if self.view == "projects":
            foot = " ↑↓:nav  Enter:open  D:delete all  q:quit"
        else:
            foot = " ↑↓:nav  Space:select  a:toggle all  d:delete  D:delete all  q:back"
        self.stdscr.addnstr(footer_line, 0, foot, max_w - 1, curses.color_pair(5))

        self.stdscr.refresh()

    def _draw_project_row(self, y: int, max_w: int, proj: ProjectInfo, is_cursor: bool):
        attr = curses.A_REVERSE if is_cursor else 0
        pointer = "▸ " if is_cursor else "  "
        size_str = human_size(proj.size).rjust(10)
        chat_count = f"{len(proj.chats)} chat(s)"
        mem_flag = " [mem]" if proj.has_memory else ""

        line = f"{pointer}{proj.friendly_name}"
        right = f"{chat_count}  {size_str}{mem_flag}"
        padding = max_w - len(line) - len(right) - 2
        if padding < 1:
            padding = 1
        full = line + " " * padding + right
        self.stdscr.addnstr(y, 0, full[:max_w - 1], max_w - 1, attr)

    def _draw_chat_row(self, y: int, max_w: int, chat: ChatInfo, is_cursor: bool):
        attr = curses.A_REVERSE if is_cursor else 0
        pointer = "▸ " if is_cursor else "  "
        check = "[x] " if chat.selected else "[ ] "
        ts = format_timestamp(chat.timestamp)
        size_str = human_size(chat.size)

        meta = f"  {ts}  {size_str}"
        preview_w = max_w - len(pointer) - len(check) - len(meta) - 2
        preview = (chat.preview or "")[:preview_w]

        line = f"{pointer}{check}{preview}"
        padding = max_w - len(line) - len(meta) - 1
        if padding < 1:
            padding = 1
        full = line + " " * padding + meta
        self.stdscr.addnstr(y, 0, full[:max_w - 1], max_w - 1, attr)


def main(stdscr):
    app = App(stdscr)
    if not app.projects:
        stdscr.addstr(0, 0, "No Claude Code conversations found.")
        stdscr.addstr(1, 0, f"Looked in: {CLAUDE_PROJECTS_DIR}")
        stdscr.addstr(2, 0, "Press any key to exit.")
        stdscr.getch()
        return
    app.run()


if __name__ == "__main__":
    if not CLAUDE_PROJECTS_DIR.is_dir():
        print(f"No Claude projects directory found at {CLAUDE_PROJECTS_DIR}")
        sys.exit(1)
    curses.wrapper(main)

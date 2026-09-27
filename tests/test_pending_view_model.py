# -*- coding: utf-8 -*-
"""小程序待处理：首次失败不得显示“暂无未结求助”；同日结案后再求助要按工单 ID 分组。"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VIEW_MODEL = PROJECT_ROOT / "miniprogram" / "utils" / "pendingViewModel.js"


def _run_node(script: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js 不可用，跳过待处理视图模型检查")
    result = subprocess.run(
        [node, "-e", script],
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout or f"exit {result.returncode}")
    return result.stdout


def test_pending_first_network_failure_is_error_not_empty():
    script = f"""
      const vm = require({json.dumps(str(VIEW_MODEL))});
      const state = vm.applyPendingFetch({{ ok: false, error: {{ kind: 'network' }} }});
      if (!state.loadError) throw new Error('first failure must set loadError');
      if (state.showEmptyOpenHelp) throw new Error('must not present empty-open-help on first failure');
      if ((state.openHelp || []).length !== 0) throw new Error('error state should not invent cards');
      console.log('ok');
    """
    assert "ok" in _run_node(script)


def test_pending_groups_new_help_request_after_close_by_id():
    script = f"""
      const vm = require({json.dumps(str(VIEW_MODEL))});
      const payload = {{
        pairs: [{{
          pair_id: 9,
          elder_label: '妈',
          today: {{
            help_requested: true,
            help_acknowledged: true,
            closed: true
          }}
        }}],
        help_requests: [
          {{ id: 'old', pair_id: 9, status: 'resolved' }},
          {{ id: 'new', pair_id: 9, status: 'requested' }}
        ]
      }};
      const openHelp = vm.openHelpFromPayload(payload);
      if (openHelp.length !== 1 || openHelp[0].id !== 'new') {{
        throw new Error('expected only the new open request, got ' + JSON.stringify(openHelp));
      }}
      const failed = vm.applyPendingFetch({{ ok: true, payload }});
      if (failed.showEmptyOpenHelp) throw new Error('new request must not look empty');
      console.log('ok');
    """
    assert "ok" in _run_node(script)


def test_pending_ack_is_open_help_not_closable():
    script = f"""
      const vm = require({json.dumps(str(VIEW_MODEL))});
      const payload = {{
        pairs: [{{
          pair_id: 3,
          elder_label: '妈',
          today: {{
            help_requested: true,
            help_acknowledged: false,
            closed: false
          }}
        }}],
        help_requests: [
          {{ id: 'pending', pair_id: 3, status: 'pending_ack' }}
        ]
      }};
      const openHelp = vm.openHelpFromPayload(payload);
      if (openHelp.length !== 1 || openHelp[0].id !== 'pending') {{
        throw new Error('pending_ack must be openHelp, got ' + JSON.stringify(openHelp));
      }}
      const grouped = vm.groupPairsByTodayFlags(payload.pairs);
      if (grouped.closable.length !== 0) {{
        throw new Error('pending_ack pair must not be closable, got ' + JSON.stringify(grouped.closable));
      }}
      const ackPayload = {{
        pairs: [{{
          pair_id: 4,
          elder_label: '爸',
          today: {{
            help_requested: true,
            help_acknowledged: true,
            closed: false
          }}
        }}],
        help_requests: [
          {{ id: 'acked', pair_id: 4, status: 'acknowledged' }}
        ]
      }};
      const ackGrouped = vm.groupPairsByTodayFlags(ackPayload.pairs);
      if (ackGrouped.closable.length !== 1) {{
        throw new Error('acknowledged pair should be closable');
      }}
      const progressPayload = {{
        pairs: [{{
          pair_id: 5,
          elder_label: '姐',
          today: {{
            help_requested: true,
            help_acknowledged: true,
            closed: false
          }}
        }}],
        help_requests: [
          {{ id: 'progress', pair_id: 5, status: 'in_progress' }}
        ]
      }};
      const progressGrouped = vm.groupPairsByTodayFlags(progressPayload.pairs);
      if (progressGrouped.closable.length !== 1) {{
        throw new Error('in_progress pair should be closable');
      }}
      console.log('ok');
    """
    assert "ok" in _run_node(script)

## [ERR-20260306-001] rsync_macos_compatibility

**Logged**: 2026-03-06T07:36:30Z
**Priority**: medium
**Status**: pending
**Area**: infra

### Summary
macOS 自带 rsync 版本过旧，不支持 `--info=progress2` 这类新参数。

### Error
```text
rsync: unrecognized option `--info=progress2,stats'
openrsync: protocol version 29
rsync version 2.6.9 compatible
```

### Context
- Command/operation attempted: 使用 `rsync` 从远程服务器全量同步网站目录到本地
- Input or parameters used: `rsync -az --partial --human-readable --info=progress2,stats --no-owner --no-group`
- Environment details if relevant: 本机为 macOS，自带 `openrsync/rsync 2.6.9 compatible`

### Suggested Fix
优先使用兼容参数组合，例如 `--progress --stats`，不要默认假设本机 rsync 支持 GNU 新参数。

### Metadata
- Reproducible: yes
- Related Files: .learnings/ERRORS.md

---

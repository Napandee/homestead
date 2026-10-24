"""Identity checks for reusing a dedicated partition or linear LV with AIO.

No partition table or LVM metadata is changed. The same checks run at review,
immediately before erasing, and before registering the blank region.
"""
import json
import re
import shlex


def inspection():
    return r'''
region_identity() {
  K=$(lsblk -dnro TYPE "$D") || fail 'Cannot read region type'
  case "$K" in part|lvm) ;; *) fail 'Only a partition or linear logical volume is supported here';; esac
  CHILDREN=$(lsblk -nrpo NAME "$D") || fail 'Cannot inspect region children'
  [ "$(printf '%s\n' "$CHILDREN" | wc -l)" -eq 1 ] && [ -n "$CHILDREN" ] || fail 'Region has child devices'
  HOLDERS=$(ls /sys/class/block/${D##*/}/holders) || fail 'Cannot inspect region holders'
  [ -z "$HOLDERS" ] || fail 'Region has holders'
  # Ancestry must resolve to one physical local disk. Reject nested storage,
  # thin pools, encryption, multipath, RAID and network-backed devices.
  A=$(lsblk -snrpo KNAME,TYPE "$D") || fail 'Cannot read region ancestry'
  [ "$(printf '%s\n' "$A" | awk '$2=="disk" {n++} END {print n+0}')" -eq 1 ] || fail 'Region must belong to one local physical disk'
  printf '%s\n' "$A" | awk '$2!="disk" && $2!="part" && $2!="lvm" {exit 1}' || fail 'Unsupported parent topology'
  PD=$(printf '%s\n' "$A" | awk '$2=="disk" {print $1}')
  TRAN=$(lsblk -dnro TRAN "$PD") || fail 'Cannot read parent transport'
  case "$TRAN" in sata|ata|sas|nvme) ;; *) fail 'Region must use a local SATA, SAS or NVMe disk';; esac
  SYS=$(readlink -f /sys/class/block/${PD##*/}/device) || fail 'Cannot resolve physical parent'
  case "$SYS" in '') fail 'Physical parent is missing';; */virtual/*) fail 'Virtual or network-backed disks cannot be converted';; esac
  PS=$(lsblk -dnro SERIAL "$PD") || fail 'Cannot read parent serial'
  PW=$(lsblk -dnro WWN "$PD") || fail 'Cannot read parent WWN'
  PS=$(printf '%s' "$PS" | sed 's/^ *//;s/ *$//'); PW=$(printf '%s' "$PW" | sed 's/^ *//;s/ *$//')
  [ -n "$PS$PW" ] || fail 'Physical parent identity is missing'
  EXTRA=''
  if [ "$K" = part ]; then
    [ "$(printf '%s\n' "$A" | wc -l)" -eq 2 ] || fail 'Partition ancestry is ambiguous'
    RID=$(lsblk -dnro PARTUUID "$D") || fail 'Cannot read partition UUID'
    [ -n "$RID" ] || fail 'Partition UUID is missing'
    STABLE="/dev/disk/by-partuuid/$RID"
  else
    command -v lvs >/dev/null || fail 'LVM inspection tool is missing'
    EXTRA=$(lvs --segments --noheadings --separator '|' -o lv_uuid,vg_uuid,segtype,lv_attr,devices "$D") || fail 'Cannot inspect logical volume'
    [ "$(printf '%s\n' "$EXTRA" | wc -l)" -eq 1 ] || fail 'Multi-segment logical volumes need manual preparation'
    printf '%s\n' "$EXTRA" | awk -F'|' '{gsub(/ /,"",$3); gsub(/ /,"",$4); if($3!="linear" || substr($4,1,1)!="-") exit 1}' || fail 'Only ordinary linear logical volumes without snapshots can be converted'
    RID=$(cat /sys/class/block/${D##*/}/dm/uuid) || fail 'Cannot read logical volume identity'
    case "$RID" in LVM-*) ;; *) fail 'Logical volume identity is missing';; esac
    STABLE="/dev/disk/by-id/dm-uuid-$RID"
  fi
  RESOLVED=$(readlink -f "$STABLE") || fail 'Cannot resolve stable region path'
  [ "$RESOLVED" = "$D" ] || fail 'Stable region path does not match'
  for p in / /boot /boot/efi /usr /var /home /var/lib/kubelet /var/lib/rancher /var/lib/homestead; do
    S=$(findmnt -rn --target "$p" -o SOURCE) || fail 'Cannot verify system storage'
    SYSTEM_DEVICE=$(readlink -f "$S") || fail 'Cannot resolve system storage'
    [ -n "$SYSTEM_DEVICE" ] && [ "$SYSTEM_DEVICE" != "$D" ] || fail 'Region contains system storage'
  done
  while read -r s rest; do
    [ "$s" = Filename ] && continue
    SWAP_DEVICE=$(readlink -f "$s") || fail 'Cannot resolve swap storage'
    [ -n "$SWAP_DEVICE" ] && [ "$SWAP_DEVICE" != "$D" ] || fail 'Region is active swap'
  done < /proc/swaps
  TOPO=$(lsblk -snrpb -o KNAME,TYPE,SIZE,SERIAL,WWN,PARTUUID,START "$D") || fail 'Cannot read region geometry'
  SIBLINGS=$(lsblk -nrpb -o KNAME,TYPE,SIZE,PARTUUID,START "$PD") || fail 'Cannot read sibling geometry'
  REGION=$(printf '%s\n%s\n%s\n%s\n' "$RID" "$TOPO" "$EXTRA" "$SIBLINGS" | sha256sum | cut -d' ' -f1)
}
region_identity
'''


def facts(lines):
    kind = lines.get('KIND')
    stable = lines.get('REGION_PATH', '')
    fingerprint = lines.get('REGION', '')
    if kind not in ('part', 'lvm') or not re.fullmatch(r'[a-f0-9]{64}', fingerprint):
        raise ValueError('Dedicated region identity and topology could not be verified')
    pattern = r'/dev/disk/by-partuuid/[A-Za-z0-9-]+' if kind == 'part' else r'/dev/disk/by-id/dm-uuid-LVM-[A-Za-z0-9-]+'
    if not re.fullmatch(pattern, stable):
        raise ValueError('A stable partition or logical-volume identity is required')
    parents = lines.get('PARENTS', '').split()
    if not parents or any(not re.fullmatch(r'/dev/[A-Za-z0-9_.-]+', p) for p in parents):
        raise ValueError('Region parent devices could not be verified')
    try:
        layout = json.loads(lines['LAYOUT'])['blockdevices']
        if len(layout) != 1 or layout[0].get('type') != 'disk':
            raise ValueError()
        def row(item):
            path = item['kname']
            size = int(item['size'])
            if not re.fullmatch(r'/dev/[A-Za-z0-9_.-]+', path) or size <= 0:
                raise ValueError()
            return {'path': path, 'kind': item['type'], 'size_bytes': size,
                    'start_bytes': int(item['start']) * 512 if item.get('start') is not None else None,
                    'mountpoint': str(item.get('mountpoint') or ''),
                    'children': [row(child) for child in item.get('children', [])]}
        layout = row(layout[0])
        physical = [item for item in layout['children'] if item['kind'] == 'part']
        if physical and len(physical) != len(layout['children']):
            raise ValueError()
        end = 0
        for part in sorted(physical, key=lambda item: item['start_bytes'] if item['start_bytes'] is not None else -1):
            start = part['start_bytes']
            if (start is None or start < end or start + part['size_bytes'] > layout['size_bytes']
                    or any(child['kind'] != 'lvm' or child['children'] for child in part['children'])):
                raise ValueError()
            end = start + part['size_bytes']
        if not physical and any(item['kind'] != 'lvm' or item['children'] for item in layout['children']):
            raise ValueError()
        def walk(item):
            return [item] + [descendant for child in item['children'] for descendant in walk(child)]
        selected = [item for item in walk(layout) if item['path'] == lines['DEVICE']]
        if len(selected) != 1 or selected[0]['kind'] != kind or selected[0]['children'] or selected[0]['size_bytes'] != int(lines['SIZE']):
            raise ValueError()
    except (KeyError, TypeError, ValueError, RecursionError):
        raise ValueError('The physical disk layout could not be verified') from None
    return {'kind': kind, 'region': fingerprint, 'by_id': stable, 'parents': parents, 'layout': layout}


def verify(facts):
    return inspection() + '\n[ "$REGION" = ' + shlex.quote(facts['region']) + ' ] || fail "Region identity or geometry changed"\n'

# Browse snapshot files

In a Longhorn volume's **Snapshots** dialog, choose **Browse files** on a ready
snapshot. Review the method, then choose **Prepare browser**. The live claim
stays attached to its workload, including containers and SMB shares. This does
not roll the volume back or stop the workload.

- **V1:** Longhorn copies the selected snapshot into a temporary volume with
  one replica. Preparation consumes disk space and I/O and can take time.
- **V2:** Homestead selects a one-replica **linked clone** when the installed
  Longhorn volume schema and CSI driver version advertise support (Longhorn
  1.10 or later). This shares snapshot blocks and avoids a full data copy.
  Scheduling and mounting still take time, and the clone depends on the source
  snapshot while open. An unrecognized driver image or unsupported schema uses
  the full-copy review instead. A failed linked clone never silently switches
  to copying after approval.

The progress view follows snapshot import, clone creation/copying, and the
read-only mount. A copy percentage appears only when Longhorn reports one;
otherwise the bar is indeterminate. Scheduling, image-pull and clone errors
are shown. Readiness requires Longhorn to report cloning complete from the
selected snapshot, a bound independent volume, and a ready helper pod.

The snapshot opens in the file manager: on the left the snapshot, read-only
and titled with when it was taken; on the right the live volume it came from,
which can be switched to another volume as usual. The snapshot pane has no
write actions at all. Files download without the live editor's size limit,
streamed through Homestead in bounded chunks; a folder, or several entries of
one folder, downloads as a zip. Symbolic links, device nodes and other special
files are not listed; a listing shows up to 500 entries. Files reflect the
snapshot's consistency, which may be crash-consistent for a running app. This
does not open a guest filesystem inside a VM disk image.

## Restore files into the live volume

Select files or folders in the snapshot pane and choose **Restore to** the
folder open on the other side. The review lists:

- what the restore **adds**, and each live file it **replaces**, with the
  live and the snapshot's size and time side by side;
- what it **skips** and why: a symbolic link that points outside the volume,
  a special file, a place where the live volume has a different kind of entry
  or a link on the way to it;
- a plain warning when an app is using the live volume: it sees the files
  change while it runs, so stop a database first. A ReadWriteOncePod volume in
  use is refused, as browsing it is.

A restore adds and replaces; it never deletes. Folders merge, files that are
not in the snapshot stay, and existing live folders keep their owner and
permissions. Symbolic links are restored as links only when they point inside
the volume. Running it plans again first: if anything reviewed changed (a live
file was written, say), it shows the new review with **Review again** instead
of running. It runs as a background copy, one restore into a volume at a time.
Closing the file manager or the snapshot while it runs does not cut it off:
the snapshot and the volume's helper stay until it ends, then the snapshot
closes. The browsing session's 30 minutes still bound it, so a review is
refused when what it copies would not fit in the time left (at a cautious
10 MiB/s, plus 5 minutes); open the snapshot again to restore then.

Closing the file manager (including X or Escape) ends the session and requests cleanup. Abandoned sessions
expire 30 minutes after preparation starts, including time spent copying.
The helper also has a Kubernetes active deadline. Cleanup resumes after a
Homestead restart and when the API becomes available, removing the helper,
temporary claim, snapshot import and temporary StorageClass in order. Normal
Kubernetes/Longhorn finalizers are respected. The original snapshot import uses
**Retain**, so removing browser resources never deletes the original snapshot.
At most four saved sessions are normally admitted at once.

This is an admin feature for unencrypted Longhorn filesystem claims outside
system namespaces. Ready CSI snapshot support must already be installed.
Raw block volumes and encrypted/custom restore classes require other tooling.
The helper uses Homestead's verified image digest, a read-only volume mount,
read-only root filesystem, no service-account token, and no privileged mode.
It must be possible to pull that image in the source claim's namespace.

Implementation references: [Longhorn CSI snapshot imports](https://longhorn.io/docs/1.13.0/snapshots-and-backups/csi-snapshot-support/csi-volume-snapshot-associated-with-longhorn-snapshot/)
and [V2 linked clones for temporary backup volumes](https://longhorn.io/blog/20250902-k8s-backup-solutions-and-longhorn/).

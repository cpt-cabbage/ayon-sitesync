# Site Sync for Artists

Since `1.3.1+ls.0.2.0`, Site Sync is designed to need **one answer** from
you, ever. Everything else is automatic.

## First launch on a new machine

1. Install the AYON launcher and log in.
2. Shortly after the tray starts you get one question:
   **"Where is this machine working from?"**
    - **In the studio** — you use files directly from the studio storage.
      Nothing changes for you; nothing syncs on this machine.
    - **Remote / from home** — you work in a local folder
      (`~/AYON_local`, created for you) and files sync with the studio in
      the background.

That's it. Your answer is remembered for this machine. If you skip the
question, Site Sync guesses by checking whether the studio storage is
reachable, and asks again next time.

## What happens automatically on a remote machine

- **Your work downloads itself.** The latest published workfile — plus
  the files it references — is fetched in the background (checked every
  5 minutes by default) for every task **assigned to you** and every
  task **you have opened** on this machine, assigned or not. Launch
  Maya, Houdini, Nuke or Blender on a task and the scene seeds itself
  from the last published workfile; opening any shot once keeps it
  syncing for the next two weeks (studio-configurable).
- **Your scenes come with you.** Work-area workfiles (the scenes you and
  your teammates save, published or not) of your assigned and opened
  tasks are copied from the studio share to your machine whenever the
  share is reachable (VPN up) — so the Workfiles list isn't greyed out
  and launching a task opens your newest scene. Your local files are
  never overwritten by this.
- **Publishing uploads itself.** When you publish, the files transfer to
  the studio in the background, starting within seconds.
- **Loading something new downloads it.** Products you download via the
  Loader (right-click → *Download*) start transferring immediately.

## The tray menu

The tray has a **Site Sync** submenu:

- **Show sync queue…** — a live window of everything queued,
  transferring (with progress) or failed on this machine, with a
  one-click *Retry all failed*.
- **Sync now** — don't wait for the next background pass.
- **Pause syncing** — temporarily stop **all** transfers, uploads
  included (e.g. on a bad connection). Uncheck to resume immediately.
- **Auto-download new work** — uncheck to stop background downloads
  only; your own publishes still upload. This is the switch to use when
  you don't want opened/assigned tasks pulling files to this machine.
- **Adopt existing local files** — if you already have files on disk
  (copied by hand, restored from a backup), this marks them as synced so
  they are not downloaded again.
- **Open sync status page…** — the web page with per-file status.

## When something goes wrong

- A failed transfer shows a **tray notification** (at most one per
  project per 5 minutes) and the file reads **Failed** on the web status
  page, with the error text shown in full on hover.
- On the web page you can **Retry all failed** (toolbar) or open a
  representation's detail and **Retry failed files**. Progress bars on
  the web page update live while transfers run.
- The tray's **Show sync queue…** window shows the same live picture
  without leaving the desktop.
- If Site Sync tells you a **hidden override disables it for this
  machine**, send that message to your admin — it is a known server-side
  trap they can remove in a minute (see the admin guide).

## Things to know

- Your local files live under `~/AYON_local/` by default. You can pick a
  different folder in your site settings on the AYON web page ("Local
  roots overrides") — avoid OneDrive/Dropbox/Google Drive folders; Site
  Sync warns if you choose one.
- Auto-download pauses itself when your disk has less than 5 GB free
  (configurable by the studio) and tells you in the log.
- Removing something from your local site (Loader → *Remove from local*)
  is respected: it will not be auto-downloaded again.
- Work-area scene fetching needs the studio share reachable (VPN). Fully
  offline, only *published* files can move — publishing is how your work
  gets back to the studio.

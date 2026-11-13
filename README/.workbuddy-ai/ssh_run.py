import sys, os, time, paramiko

host = sys.argv[1]
user = sys.argv[2]
pwd = sys.argv[3]
cmd = sys.stdin.read() if len(sys.argv) < 5 else sys.argv[4]
port = int(os.environ.get("SSH_PORT", "22"))

t = paramiko.Transport((host, port))
t.start_client(timeout=20)
banner = t.get_banner()
if banner:
    sys.stderr.write("BANNER: %s\n" % banner.decode("utf-8", "replace").strip())

authed = False
try:
    t.auth_password(user, pwd)
    authed = True
    sys.stderr.write("AUTH: password OK\n")
except Exception as e:
    sys.stderr.write("AUTH: password failed (%s)\n" % type(e).__name__)
if not authed:
    try:
        def handler(title, instructions, prompt_list):
            return [pwd for _ in prompt_list]
        t.auth_interactive(user, handler)
        authed = True
        sys.stderr.write("AUTH: keyboard-interactive OK\n")
    except Exception as e:
        sys.stderr.write("AUTH: keyboard-interactive failed (%s)\n" % type(e).__name__)
if not authed:
    sys.stderr.write("AUTH: all methods failed\n")
    sys.exit(2)

try:
    chan = t.open_session()
    chan.settimeout(0.1)
    chan.exec_command('export LANG=C.UTF-8 LC_ALL=C.UTF-8; ' + cmd)
    out = b""
    err = b""
    while True:
        changed = False
        while chan.recv_ready():
            out += chan.recv(65536)
            changed = True
        while chan.recv_stderr_ready():
            err += chan.recv_stderr(65536)
            changed = True
        if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
            break
        if not changed:
            time.sleep(0.1)
    rc = chan.recv_exit_status()
    sys.stdout.write(out.decode("utf-8", "replace"))
    if err:
        sys.stdout.write("\n===== STDERR =====\n" + err.decode("utf-8", "replace"))
    sys.stdout.write("\n===== EXIT %d =====\n" % rc)
finally:
    t.close()

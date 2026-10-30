"""Deploy helper — runs remote commands over SSH and uploads the penora tarball.

Usage (from the penora venv):
    python deploy.py upload            # upload + extract penora to ~/penora
    python deploy.py run "cmd"         # run a remote command, stream output
    python deploy.py write path < file # write stdin to remote path (via base64)
"""
import base64
import getpass
import os
import sys
import paramiko

HOST, USER = "173.249.6.22", "hunter"
PW = os.environ.get("VPS_PW") or getpass.getpass("VPS password: ")

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PW, timeout=15)


def run(cmd, timeout=600):
    print(f"$ {cmd}")
    stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    rc = stdout.channel.recv_exit_status()
    if out:
        print(out, end="" if out.endswith("\n") else "\n")
    if err:
        print("STDERR:", err[:2000])
    if rc != 0:
        print(f"[exit {rc}]")
    return rc, out, err


def upload(local, remote):
    sftp = c.open_sftp()
    sftp.put(local, remote)
    sftp.close()
    print(f"uploaded {local} -> {remote}")


def write_text(remote, text):
    b64 = base64.b64encode(text.encode()).decode()
    run(f"echo {b64} | base64 -d > {remote}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "upload":
        sftp = c.open_sftp()
        run("mkdir -p ~/penora && rm -f /tmp/penora.tar.gz")
        upload("/tmp/penora.tar.gz", "/tmp/penora.tar.gz")
        run("cd ~ && rm -rf penora && mkdir -p penora && tar -xzf /tmp/penora.tar.gz -C ~ --strip-components=1 && find penora -type f | wc -l")
    elif mode == "run":
        run(" ".join(sys.argv[2:]), timeout=int(os.environ.get("RC_TIMEOUT", "600")))
    elif mode == "write":
        write_text(sys.argv[2], sys.stdin.read())
    elif mode == "upload_file":
        upload(sys.argv[2], sys.argv[3])
    c.close()

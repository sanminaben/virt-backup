import os
import re
import shutil
import tarfile


def copy_file(src, dst, buffersize=None):
    if not os.path.exists(dst) and dst.endswith("/"):
        os.makedirs(dst)
    if os.path.isdir(dst):
        dst = os.path.join(dst, os.path.basename(src))

    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        shutil.copyfileobj(fsrc, fdst, buffersize)
    return dst

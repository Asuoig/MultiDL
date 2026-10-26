"""生成测试用大文件（40 MB 随机数据）+ 记录 sha256。"""
import os
import sys
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "big.bin")
SIZE = 40 * 1024 * 1024

if not os.path.exists(PATH) or os.path.getsize(PATH) != SIZE:
    with open(PATH, "wb") as f:
        for _ in range(SIZE // (1024 * 1024)):
            f.write(os.urandom(1024 * 1024))
    print("created", PATH, SIZE)

h = hashlib.sha256()
with open(PATH, "rb") as f:
    for b in iter(lambda: f.read(1 << 20), b""):
        h.update(b)
print("sha256:", h.hexdigest())

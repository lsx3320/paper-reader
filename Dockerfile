FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    TZ=Asia/Shanghai

# 可用 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple 换国内源
ARG PIP_INDEX_URL=https://pypi.org/simple

WORKDIR /srv

# PyMuPDF 官方 wheel 自带 MuPDF，无需系统依赖：不装 apt 包，构建更快也不依赖 Debian 源
COPY requirements.txt .
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r requirements.txt

COPY app ./app

RUN mkdir -p /data/pdfs \
 && useradd -m -u 10001 reader \
 && chown -R reader:reader /data /srv
USER reader

EXPOSE 8000

# 健康检查用镜像内自带的 python，避免依赖 curl
HEALTHCHECK --interval=30s --timeout=6s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]

# The image the Hugging Face Space runs. Built by Hugging Face, not by you --
# nothing here needs Docker installed locally. Pushing to the Space's git remote
# is what triggers a build.
FROM python:3.11-slim

# Spaces run the container as uid 1000, not root. Creating that user here rather
# than letting it be imposed means HOME, the pip target and the model cache all
# belong to the account that will actually run the process. Left as root, the
# first thing the app does -- write a downloaded model into ~/.cache -- fails.
RUN useradd --create-home --uid 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    HF_HOME=/home/user/.cache/huggingface \
    PYTHONUNBUFFERED=1
WORKDIR /home/user/app

COPY --chown=user requirements-website.txt .

# CPU wheel explicitly, and before the rest. The default index serves a CUDA
# build: 2.5 GB of driver support for a machine with no graphics card.
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements-website.txt

# The three models are downloaded into the image rather than at startup.
#
# It makes the build slower and the image about 5 GB. The alternative is worse:
# a free Space sleeps when idle, and every wake would spend several minutes
# fetching 3.5 GB before it could answer anything -- the first visitor after a
# quiet night would sit looking at nothing. Baked in, a wake is a process start.
#
# This is also the layer that makes rebuilds cheap. It sits above the source
# copy, so editing the app or the page rebuilds only the two layers below it.
RUN python -c "\
from sentence_transformers import SentenceTransformer, CrossEncoder; \
from fastembed import SparseTextEmbedding; \
SentenceTransformer('intfloat/multilingual-e5-base'); \
CrossEncoder('BAAI/bge-reranker-v2-m3', max_length=512); \
SparseTextEmbedding('Qdrant/bm25'); \
print('models cached')"

COPY --chown=user website_search.py website_app.py ./
COPY --chown=user templates/website.html templates/

# 7860 is the port Spaces expect. Anything else is reachable only from inside
# the container, which looks exactly like a Space that will not start.
EXPOSE 7860
CMD ["uvicorn", "website_app:app", "--host", "0.0.0.0", "--port", "7860"]

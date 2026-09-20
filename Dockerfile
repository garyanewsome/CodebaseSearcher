FROM python:3.12-slim

# git + openssh-client: repo_manager.py and vault_writer.py both shell out
# to the real `git` CLI over SSH rather than a Python git library — same
# reasoning as Athenaeum's sync.py, simpler and more obviously correct
# than wrapping libgit2/dulwich for a task this straightforward.
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

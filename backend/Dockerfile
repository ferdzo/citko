FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY citko ./citko
COPY alembic ./alembic
COPY alembic.ini .
RUN chown -R 10001:10001 /app
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn","citko.main:app","--host","0.0.0.0","--port","8000"]

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY mock_bank ./mock_bank
COPY migrations ./migrations
# Never run as root inside the container.
RUN useradd --create-home appuser
USER appuser
EXPOSE 8000
ENTRYPOINT ["python", "-m", "app.main"]
CMD ["api"]

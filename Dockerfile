FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 5000
CMD ["python", "app.py"]
```

**Step 4:** Create a file named `.dockerignore` and paste this:
```
__pycache__/
*.pyc
.git/
.env
static/qrcodes/
static/uploads/
FROM python:3.12-slim

WORKDIR /app

# Cài dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY budget_bot.py .

# Tạo user không có quyền root
RUN useradd -m botuser
USER botuser

CMD ["python", "budget_bot.py"]
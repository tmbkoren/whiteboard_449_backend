# Dockerfile

# Use a lightweight Python base image (e.g., Python 3.12)
FROM python:3.12-slim

# Set the working directory inside the container
# All subsequent commands will be run from this directory
WORKDIR /app

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install the Python dependencies
# The --no-cache-dir flag helps keep the image size down
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code into the container at /app
COPY . .

# Expose the port that Uvicorn will run on
# This tells Docker that the container listens on this port
EXPOSE 8000

# Define the command to run your application when the container starts
# `uvicorn main:app` means run the `app` object from `main.py`
# `--host 0.0.0.0` makes the application accessible from outside the container
# `--port 8000` specifies the port Uvicorn will listen on
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

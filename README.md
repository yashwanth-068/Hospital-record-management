# Hospital Management System (HMS)
## Full Stack Web Application with DevOps Integration

A comprehensive hospital management system built with Flask, MySQL, and deployed using Docker and Kubernetes for monitoring with Prometheus and Grafana.

---

## 🏥 Features

- **Multi-Role Authentication**: Admin, Doctor, and Patient portals
- **Patient Health Records**: Digital health cards with QR codes
- **Appointment Management**: Book, confirm, and track appointments
- **Medication Tracking**: Scheduled medication with compliance monitoring
- **Discharge & Billing**: GST-compliant billing with discharge summaries
- **Doctor Availability**: Real-time appointment slot management

---

## 🛠️ Tech Stack

- **Backend**: Flask (Python)
- **Database**: MySQL 8.0
- **Authentication**: bcrypt password hashing
- **QR Codes**: Patient card generation
- **Containerization**: Docker & Docker Compose
- **Orchestration**: Kubernetes (Minikube)
- **Monitoring**: Prometheus & Grafana

---

## ⚙️ Setup Instructions

### Prerequisites

- Python 3.10+
- MySQL 8.0
- Docker Desktop
- kubectl
- Minikube
- Helm

### Local Development Setup

1. **Clone the repository**
   ```bash
   git clone <your-repo-url>
   cd "hmss mere near - Copy"
   ```

2. **Create virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables**
   ```bash
   cp .env.example .env
   # Edit .env with your actual database credentials
   ```

5. **Setup MySQL database**
   - Create database: `health_record`
   - Import schema (create tables for hospitals, admins, doctors, patients, etc.)

6. **Run the application**
   ```bash
   python app.py
   ```
   Access at: `http://localhost:5000`

---

## 🐳 Docker Deployment

### Build and run with Docker Compose

```bash
# Build the Docker image
docker build -t hospital-app:latest .

# Start MySQL + Flask app
docker-compose up -d

# Check running containers
docker ps

# View logs
docker-compose logs -f

# Stop services
docker-compose down
```

Access at: `http://localhost:5000`

---

## ☸️ Kubernetes Deployment (DevOps Project)

### Step 1: Start Minikube
```bash
minikube start --driver=docker --memory=6144 --cpus=2
```

### Step 2: Build image inside Minikube
```bash
minikube image build -t hospital-app:latest .
```

### Step 3: Deploy MySQL
```bash
kubectl apply -f k8s/mysql-deployment.yaml
kubectl get pods  # Wait for MySQL to be Running
```

### Step 4: Deploy Flask App
```bash
kubectl apply -f k8s/app-deployment.yaml
kubectl get pods  # Wait for hospital-app pods to be Running
```

### Step 5: Access the application
```bash
minikube service hospital-app --url
```

---

## 📊 Monitoring with Prometheus & Grafana

### Install kube-prometheus-stack

```bash
# Add Prometheus Helm repo
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

# Create monitoring namespace
kubectl create namespace monitoring

# Install stack
helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --set grafana.adminPassword=admin123
```

### Access Grafana Dashboard

```bash
# Port-forward Grafana
kubectl port-forward svc/kube-prometheus-stack-grafana 3000:80 -n monitoring
```

Open browser: `http://localhost:3000`
- **Username**: admin
- **Password**: admin123

### Import Dashboard
- Dashboard ID: **15661** (Kubernetes Cluster Overview)
- Data Source: Prometheus

---

## 🔒 Security Notes

⚠️ **IMPORTANT**: This repository does NOT contain:
- Actual database passwords (use environment variables)
- Patient QR codes (excluded via .gitignore)
- Uploaded patient files (excluded via .gitignore)
- Production secrets (use .env file)

**Before deploying to production:**
1. Change all default passwords
2. Use proper secret management (Kubernetes Secrets, AWS Secrets Manager)
3. Enable HTTPS/TLS
4. Set `debug=False` in Flask
5. Implement rate limiting and CSRF protection

---

## 📁 Project Structure

```
hmss mere near - Copy/
├── app.py                  # Main Flask application
├── requirements.txt        # Python dependencies
├── Dockerfile             # Docker build instructions
├── docker-compose.yml     # Multi-container setup
├── .dockerignore          # Docker ignore rules
├── .gitignore            # Git ignore rules
├── .env.example          # Environment variables template
├── k8s/                  # Kubernetes manifests
│   ├── mysql-deployment.yaml
│   └── app-deployment.yaml
├── static/               # Static assets
│   ├── css/
│   ├── qrcodes/         # Generated QR codes (not tracked)
│   └── uploads/         # User uploads (not tracked)
└── templates/            # HTML templates
    ├── admin_*.html
    ├── doctor_*.html
    └── patient_*.html
```

---

## 👥 User Roles & Access

| Role | Features |
|------|----------|
| **Admin** | Add doctors/patients, discharge patients, billing, hospital settings |
| **Doctor** | View assigned patients, add health records, medication schedules, appointments |
| **Patient** | View health records, book appointments, log medication intake, download QR card |

---

## 📝 Database Schema

Key tables:
- `hospitals` - Hospital registration
- `admins` - Admin accounts
- `doctors` - Doctor profiles
- `patients` - Patient records
- `health_records` - Medical consultation notes
- `medication_schedule` - Prescribed medications
- `appointments` - Appointment bookings
- `discharge_summary` - Discharge records with billing
- `doctor_availability` - Real-time availability status
- `pill_intake_log` - Medication compliance tracking

---

## 🎓 College Project - DevOps Task

This project demonstrates:
1. ✅ Containerization (Docker)
2. ✅ Container orchestration (Kubernetes)
3. ✅ Service deployment (MySQL + Flask)
4. ✅ Monitoring setup (Prometheus + Grafana)
5. ✅ Performance metrics visualization
6. ✅ GitOps workflow

### Monitoring Metrics Tracked:
- CPU usage per pod
- Memory consumption
- Network I/O
- Pod restart count
- Request latency
- Database connection pool

---

## 🐛 Troubleshooting

**Issue**: Pods stuck in `Pending`
- **Solution**: Increase Minikube memory: `minikube start --memory=8192`

**Issue**: `ImagePullBackOff`
- **Solution**: Rebuild image inside Minikube: `minikube image build -t hospital-app:latest .`

**Issue**: Database connection failed
- **Solution**: Ensure MySQL pod is running: `kubectl get pods`, wait for `Running` status

**Issue**: Port-forward disconnects
- **Solution**: This is normal, just re-run the port-forward command

---

## 📄 License

This is a college project for educational purposes.

---

## 👨‍💻 Author

College DevOps Mini Project - Kubernetes Performance Metrics with Prometheus and Grafana

---

## 🙏 Acknowledgments

- Flask documentation
- Kubernetes official docs
- Prometheus & Grafana communities

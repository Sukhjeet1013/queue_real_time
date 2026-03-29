# SmartQueue

A clinic queue management system that provides transparent, data-driven wait time estimates and live queue tracking for patients and clinic staff.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## 📋 Table of Contents

- [Problem Statement](#problem-statement)
- [Solution](#solution)
- [Key Features](#key-features)
- [How It Works](#how-it-works)
- [System Architecture](#system-architecture)
- [Tech Stack](#tech-stack)
- [Getting Started](#getting-started)
- [Current Limitations](#current-limitations)
- [Future Roadmap](#future-roadmap)
- [License](#license)

---

## 🎯 Problem Statement

Traditional clinic queuing systems leave patients in the dark. They receive a token number but have no visibility into:

- **How many patients are ahead of them** – Is it 2 or 20?
- **How long they'll actually wait** – 10 minutes or 2 hours?
- **Whether the queue is moving or stalled** – Should they grab lunch or stay seated?

This creates anxiety, frustration, and inefficient use of patients' time. Clinics, meanwhile, lack tools to communicate queue status effectively, leading to overcrowded waiting rooms and repeated inquiries to front desk staff.

---

## 💡 Solution

SmartQueue improves the waiting experience by providing **queue visibility** and **estimated wait times**. Patients can see their position in the queue in real-time, while clinic administrators manage patient flow through a centralized dashboard.

Instead of static estimates, SmartQueue uses **historical consultation data** and **weighted averaging** to calculate wait times dynamically, providing patients with accurate, personalized predictions.

---

## ✨ Key Features

### For Patients

- **Live Queue Status** – See your current position in the queue
- **Position Tracking** – Know exactly how many patients are ahead
- **Estimated Wait Time** – Get data-driven predictions based on historical patterns
- **Browser-Based Access** – No app download required, works on any device

### For Clinic Staff

- **Admin Dashboard** – Centralized queue management interface
- **Role-Based Access** – Control permissions for different staff members
- **Multi-Clinic Support** – Manage multiple clinic locations from one system

### Security

- **Flask-Login Authentication** – Secure user sessions
- **Role-Based Access Control (RBAC)** – Granular permission management

---

## 🔍 How Wait Time Estimation Works

SmartQueue calculates wait times using historical consultation data with weighted averaging:

```
Example:
├─ Patients ahead: 5
├─ Estimated avg consultation time: ~8.6 min
└─ Total estimated wait: ≈ 43 minutes
```

The system analyzes past consultation durations and applies weighted averages to account for variability, providing more accurate predictions than simple fixed-time estimates.

---

## ⚙️ Core Design Decisions

- Enforced strict queue state transitions:
  `waiting → in_consultation → served`

- Ensured only one active consultation per clinic using database-level constraints

- Token numbers are unique per clinic to support multi-clinic scalability

- Time tracking implemented using timezone-aware timestamps (IST)

- Role-based access control separates system-level admins and clinic-level admins

---

## 🏗️ System Architecture

```
Patient (Browser)
    ↓
Flask Backend (REST API)
    ↓
PostgreSQL Database
```

**Flow:**
1. Patient accesses queue via browser
2. Flask backend processes requests and calculates wait times
3. PostgreSQL stores queue data, patient information, and historical metrics
4. Queue updates delivered via periodic refresh (real-time updates planned)

---

## 🛠️ Tech Stack

- **Backend:** Flask (Python web framework)
- **Database:** PostgreSQL (Relational database)
- **ORM:** SQLAlchemy (Database abstraction layer)
- **Authentication:** Flask-Login (Session management)
- **Deployment:** Railway (Cloud hosting platform)

---

## 🚀 Getting Started

### Prerequisites

- Python 3.8+
- PostgreSQL
- pip

### Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/yourusername/smartqueue.git
   cd smartqueue
   ```

2. **Create a virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up environment variables**
   
   Create a `.env` file in the project root:
   ```env
   DATABASE_URL=postgresql://username:password@localhost:5432/smartqueue
   SECRET_KEY=your-secret-key-here
   ```

5. **Initialize the database**
   ```bash
   flask db upgrade
   ```

6. **Run the application**
   ```bash
   flask run
   ```

7. **Access the application**
   
   Open your browser and navigate to: `http://localhost:5000`

---

## ⚠️ Current Limitations

- **No Real-Time Updates** – Queue status requires manual refresh
- **No Location Support** – GPS-based proximity features not yet implemented
- **Not ML-Based** – Uses statistical averaging rather than machine learning predictions

---

## 🗺️ Future Roadmap

- [ ] **Push Notifications** – Alert patients when their turn is approaching
- [ ] **Mobile Application** – Native iOS and Android apps
- [ ] **Advanced Analytics** – Dashboard insights for clinic optimization
- [ ] **Machine Learning** – Predictive models for more accurate wait time estimation
- [ ] **Multi-Language Support** – Localization for diverse patient populations
- [ ] **SMS Integration** – Text-based queue updates

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

---

## 📧 Contact

For questions or support, please open an issue on GitHub.

---

**Made with ❤️ for better healthcare experiences**
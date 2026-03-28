# (ONLY showing modified parts clearly — rest stays same structure)

# -----------------------------
# FIX 1: WAIT TIME LOGIC
# -----------------------------
def compute_estimated_wait_minutes(entry):
    if entry.status != QueueEntry.STATUS_WAITING:
        return 0 if entry.status == QueueEntry.STATUS_IN_CONSULTATION else None

    recent_served_entries = (
        db.session.execute(
            select(QueueEntry)
            .where(
                QueueEntry.clinic_id == entry.clinic_id,
                QueueEntry.status == QueueEntry.STATUS_SERVED,
                QueueEntry.consultation_started_at.is_not(None),
                QueueEntry.served_at.is_not(None),
            )
            .order_by(QueueEntry.served_at.desc())
            .limit(10)
        )
        .scalars()
        .all()
    )

    durations = []
    for served_entry in recent_served_entries:
        duration = served_entry.served_at - served_entry.consultation_started_at

        # ✅ FIXED: never allow 0 duration
        duration_minutes = max(1, int(duration.total_seconds() // 60))
        durations.append(duration_minutes)

    # ✅ FIXED: fallback if no history
    patients_ahead = db.session.scalar(
        select(func.count(QueueEntry.id)).where(
            QueueEntry.clinic_id == entry.clinic_id,
            QueueEntry.status == QueueEntry.STATUS_WAITING,
            QueueEntry.token_number < entry.token_number,
        )
    ) or 0

    has_active = db.session.scalar(
        select(func.count(QueueEntry.id)).where(
            QueueEntry.clinic_id == entry.clinic_id,
            QueueEntry.status == QueueEntry.STATUS_IN_CONSULTATION,
        )
    )

    slots_ahead = patients_ahead + (1 if has_active else 0)

    if not durations:
        return 5 * slots_ahead  # ✅ fallback logic

    avg = round(sum(durations) / len(durations))
    return avg * slots_ahead


# -----------------------------
# FIX 2: CALL NEXT (REMOVE LOCKS)
# -----------------------------
@app.route("/call_next/<int:clinic_id>", methods=["POST"])
@clinic_admin_required
def call_next(clinic_id):
    if clinic_id != current_user.clinic_id:
        abort(403)

    clinic = db.session.get(Clinic, clinic_id)
    if not clinic:
        abort(404)

    active_entry = db.session.execute(
        select(QueueEntry)
        .where(
            QueueEntry.clinic_id == clinic_id,
            QueueEntry.status == QueueEntry.STATUS_IN_CONSULTATION,
        )
        .limit(1)
    ).scalar_one_or_none()

    if active_entry:
        flash("A patient is already in consultation.", "warning")
        return redirect(url_for("admin_dashboard"))

    next_entry = db.session.execute(
        select(QueueEntry)
        .where(
            QueueEntry.clinic_id == clinic_id,
            QueueEntry.status == QueueEntry.STATUS_WAITING,
        )
        .order_by(QueueEntry.token_number.asc())
        .limit(1)
    ).scalar_one_or_none()

    if not next_entry:
        flash("No patients waiting.", "info")
        return redirect(url_for("admin_dashboard"))

    next_entry.status = QueueEntry.STATUS_IN_CONSULTATION
    next_entry.consultation_started_at = datetime.utcnow()

    db.session.commit()

    flash(f"Token {next_entry.token_number} is now in consultation.", "success")
    return redirect(url_for("admin_dashboard"))


# -----------------------------
# FIX 3: MARK SERVED (REMOVE LOCK)
# -----------------------------
@app.route("/mark_served/<int:entry_id>", methods=["POST"])
@app.route("/complete/<int:entry_id>", methods=["POST"])
@clinic_admin_required
def mark_served(entry_id):

    entry = db.session.execute(
        select(QueueEntry)
        .options(joinedload(QueueEntry.patient))
        .where(QueueEntry.id == entry_id)
    ).scalar_one_or_none()

    if not entry:
        abort(404)

    if entry.clinic_id != current_user.clinic_id:
        abort(403)

    if entry.status != QueueEntry.STATUS_IN_CONSULTATION:
        flash("Only active consultation can be marked served.", "warning")
        return redirect(url_for("admin_dashboard"))

    entry.status = QueueEntry.STATUS_SERVED
    entry.served_at = datetime.utcnow()

    if entry.consultation_started_at is None:
        entry.consultation_started_at = entry.served_at

    db.session.commit()

    flash(f"Token {entry.token_number} marked served.", "success")
    return redirect(url_for("admin_dashboard"))


# -----------------------------
# FIX 4: REMOVE DUPLICATE BOOTSTRAP
# -----------------------------

# ❌ REMOVE THIS BLOCK COMPLETELY:
# with app.app_context():
#     bootstrap_database()

# -----------------------------
# KEEP ONLY THIS:
# -----------------------------
if __name__ != "__main__":
    with app.app_context():
        bootstrap_database()

if __name__ == "__main__":
    app.run(debug=True)
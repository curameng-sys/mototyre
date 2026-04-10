from app import db
from datetime import datetime

class Notification(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title      = db.Column(db.String(120), nullable=False)
    message    = db.Column(db.Text, nullable=False)
    type       = db.Column(db.String(30), default='update')
    # type options: booking, order, mechanic, cost, update, reminder
    status     = db.Column(db.String(30), nullable=True)
    # status options: pending, confirmed, inprogress, completed, cancelled, delivered
    is_read    = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
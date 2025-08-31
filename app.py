from flask import Flask, render_template, request, redirect, session, url_for, jsonify, flash, send_file
from pymongo import MongoClient
from datetime import datetime
from bson.objectid import ObjectId
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import timedelta
from flask_wtf.csrf import CSRFProtect
from dotenv import load_dotenv
from flask_socketio import SocketIO, emit
import os
from werkzeug.utils import secure_filename


# Load environment variables
load_dotenv('Pass.env')
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY')
csrf = CSRFProtect(app)
app.permanent_session_lifetime = timedelta(hours=1)  # Session expires after 1 hour

client = MongoClient(os.getenv('MONGO_URI'))
db = client['task_manager']
# Create notifications collection
notifications_collection = db['notifications']

tasks_collection = db['tasks']
users_collection = db['users']
socketio = SocketIO(app, cors_allowed_origins="*")

# Socket.IO connection handler
@socketio.on('connect')
def handle_connect():
    if 'user_id' in session:
        user_id = session['user_id']
        # Join a room specific to this user
        from flask_socketio import join_room
        join_room(user_id)
        print(f"User {user_id} connected to Socket.IO")
        emit('connected', {'message': 'Connected to real-time updates'}, room=user_id)


# Configure file uploads
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'doc', 'docx', 'zip'}
MAX_FILE_SIZE = 16 * 1024 * 1024  # 16MB max file size

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE

# Create uploads directory if it doesn't exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/')
def index():
    if 'username' not in session:
        return redirect(url_for('login'))
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']
    status_filter = request.args.get('status')
    search_query = request.args.get('search')
    sort_by_deadline = request.args.get('sort') == 'deadline'

    # Store the current filter in session for persistence
    if status_filter:
        session['last_filter'] = status_filter
    elif 'last_filter' in session:
        status_filter = session['last_filter']
    else:
        status_filter = None

    # Get counts for ALL tasks (always calculate these)
    total_count = tasks_collection.count_documents({'user_id': user_id})
    Pending_count = tasks_collection.count_documents({'user_id': user_id, 'status': 'Pending'})
    In_Progress_count = tasks_collection.count_documents({'user_id': user_id, 'status': 'In Progress'})
    Completed_count = tasks_collection.count_documents({'user_id': user_id, 'status': 'Completed'})

    tasks = []
    # Show components if either a filter or search is selected OR if we have a stored filter
    show_components = status_filter is not None or search_query is not None or 'last_filter' in session

    query = {"user_id": user_id}

    if status_filter and status_filter != 'all':
        query['status'] = status_filter

    if search_query:
        query['title'] = {'$regex': search_query, '$options': 'i'}  # Case-insensitive regex search

    if sort_by_deadline:
        tasks_cursor = tasks_collection.find(query).sort('deadline', 1)
    else:
        tasks_cursor = tasks_collection.find(query)

    for task in tasks_cursor:
        task['id'] = str(task['_id'])
        del task['_id']
        tasks.append(task)

    # Calculate overall completion percentage (always calculate)
    overall_completed_percentage = 0
    if total_count > 0:
        overall_completed_percentage = round((Completed_count / total_count) * 100)

    return render_template('index.html',
                           tasks=tasks,
                           selected_status=status_filter,
                           total_count=total_count,
                           Pending_count=Pending_count,
                           In_Progress_count=In_Progress_count,
                           Completed_count=Completed_count,
                           overall_completed_percentage=overall_completed_percentage,
                           username=session.get('username'),
                           show_components=show_components,
                           search_query=search_query)  # Pass search_query to template
    # Helper function to convert MongoDB documents to JSON-safe format
def serialize_task(task):
    return {
        "_id": str(task["_id"]),
        "title": task.get("title", ""),
        "description": task.get("description", ""),
        "status": task.get("status", ""),
        "created_at": task.get("created_at", ""),
        "deadline": task.get("deadline", "")
    }
def create_notification(user_id, message, notification_type='info', related_task=None):
    """Create a notification and save to database"""
    notification = {
        'user_id': user_id,
        'message': message,
        'type': notification_type,  # info, success, warning, danger
        'is_read': False,
        'created_at': datetime.utcnow(),
        'related_task': related_task  # Optional: task ID if related to a task
    }
    return notifications_collection.insert_one(notification)

# Route to fetch all tasks, with optional status filter
@app.route('/api/tasks')
def get_all_tasks():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']
    status = request.args.get('status')
    query = {"user_id": user_id}
    if status:
        query["status"] = status

    tasks_cursor = tasks_collection.find(query)
    tasks = [serialize_task(task) for task in tasks_cursor]
    return jsonify(tasks)

def convert_objectid(data):
    if isinstance(data, list):
        return [{**item, '_id': str(item['_id'])} for item in data]
    elif isinstance(data, dict):
        data['_id'] = str(data['_id'])
        return data
    return data


@app.route('/delete_task/<task_id>', methods=['POST'])  # ← ADD methods=['POST']
def delete_task(task_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']
    result = tasks_collection.delete_one({'_id': ObjectId(task_id), 'user_id': user_id})
    create_notification(
        user_id=user_id,
        message=f"Task deleted successfully!",
        notification_type='warning'
    )
    if result.deleted_count == 0:
        return "Unauthorized or task not found", 403
    if 'last_filter' in session:
        return redirect(url_for('index', status=session['last_filter']))
    return redirect(url_for('index'))  # No filter

@app.route("/update", methods=["POST"])
def update_task():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']
    task_id = request.form.get("task_id")

    # Check if task belongs to current user
    task = tasks_collection.find_one({"_id": ObjectId(task_id), "user_id": user_id})
    if not task:
        return "Unauthorized or task not found", 403

    title = request.form.get("title")
    description = request.form.get("description")
    deadline = request.form.get("deadline")
    status = request.form.get("status")

    tasks_collection.update_one(
        {"_id": ObjectId(task_id), "user_id": user_id},
        {"$set": {
            "title": title,
            "description": description,
            "deadline": deadline,
            "status": status
        }}
    )
    create_notification(
        user_id=user_id,
        message=f"Task '{title}' updated successfully!",
        notification_type='success'
    )
    if 'last_filter' in session:
        return redirect(url_for('index', status=session['last_filter']))
    return redirect(url_for('index'))  # No filter
@app.route('/filter_tasks')
def filter_tasks():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']
    status_filter = request.args.get('status')

    query = {'user_id': user_id}
    if status_filter:
        query['status'] = status_filter

    tasks = list(tasks_collection.find(query))

    for task in tasks:
        task['_id'] = str(task['_id'])

    return render_template('index.html', tasks=tasks, active_filter=status_filter or 'all')


@app.route('/add_task', methods=['POST'])
def add_task():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    # Get form data
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()
    deadline = request.form.get('deadline')
    user_id = session['user_id']

    # Server-side validation
    errors = []

    if not title:
        errors.append('Task title is required')
    if not description:
        errors.append('Task description is required')
    if deadline:
        try:
            deadline_date = datetime.strptime(deadline, '%Y-%m-%d')
            today = datetime.utcnow().date()
            if deadline_date.date() < today:
                errors.append('Deadline cannot be in the past')
        except ValueError:
            errors.append('Invalid date format')

    if errors:
        for error in errors:
            flash(error, 'error')
        return redirect(url_for('index'))

    # Only proceed if validation passes
    task = {
        'title': title,
        'description': description,
        'deadline': deadline,
        'status': 'Pending',
        'created_at': datetime.utcnow(),
        'user_id': user_id,
        'sharedWith': []  # ← ADD THIS: Array of user IDs who can access this task
    }

    tasks_collection.insert_one(task)
    create_notification(
        user_id=user_id,
        message=f"Task '{title}' created successfully!",
        notification_type='success'
    )
    flash('Task added successfully!', 'success')
    if 'last_filter' in session:
        return redirect(url_for('index', status=session['last_filter']))
    return redirect(url_for('index'))  # No filter

@app.route('/register', methods=['GET', 'POST'])
def register():
    if 'username' in session:
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form['username']  # Changed from get() to []
        password = request.form['password']
        confirm_password = request.form['confirm_password']

        if password != confirm_password:
            flash('Passwords do not match', 'error')
            return redirect(url_for('register'))

        if users_collection.find_one({'username': username}):
            flash('Username already exists', 'error')
            return redirect(url_for('register'))

        if len(password) < 8:
            flash('Password must be at least 8 characters', 'error')
            return redirect(url_for('register'))

        hashed_pw = generate_password_hash(password)
        result = users_collection.insert_one({
            'username': username,
            'password': hashed_pw
        })

        session['username'] = username
        session['user_id'] = str(result.inserted_id)
        return redirect(url_for('index'))

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'username' in session:
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        user = users_collection.find_one({'username': username})
        if user and check_password_hash(user['password'], password):
            session['username'] = username
            session['user_id'] = str(user['_id'])
            return redirect(url_for('index'))
        else:
            flash('Invalid username or password', 'error')  # Error toast

    return render_template('login.html')

@app.route('/logout', methods=['POST'])
def logout():
    session.pop('username', None)
    session.pop('user_id', None)
    return redirect('/login')


# Share task with other users
@app.route('/tasks/<task_id>/share', methods=['POST'])
def share_task(task_id):
    try:
        if 'user_id' not in session:
            return jsonify({'error': 'Not authenticated'}), 401

        target_username = request.form.get('username')

        if not target_username:
            return jsonify({'error': 'Username is required'}), 400

        # Find target user
        target_user = users_collection.find_one({'username': target_username})
        if not target_user:
            return jsonify({'error': 'User not found'}), 404

        # Verify current user owns the task
        task = tasks_collection.find_one({'_id': ObjectId(task_id), 'user_id': session['user_id']})
        if not task:
            return jsonify({'error': 'Task not found or unauthorized'}), 404

        # Add target user to sharedWith array (avoid duplicates)
        result = tasks_collection.update_one(
            {'_id': ObjectId(task_id)},
            {'$addToSet': {'sharedWith': str(target_user['_id'])}}
        )
        # Create notification for RECIPIENT
        create_notification(
            user_id=str(target_user['_id']),
            message=f"{session['username']} shared a task with you: '{task['title']}'",
            notification_type='info',
            related_task=task_id
        )

        # Create notification for SENDER (optional)
        create_notification(
            user_id=session['user_id'],
            message=f"You shared task '{task['title']}' with {target_username}",
            notification_type='success'
        )

        # In your share_task function, replace the socketio.emit call with:
        socketio.emit('notification', {
            'message': f'{session["username"]} shared a task with you: {task["title"]}',
            'type': 'info'
        }, room=str(target_user['_id']))  # Send to specific user's room

        return jsonify({'message': f'Task shared with {target_username}'})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Get tasks shared with current user
@app.route('/tasks/shared')
def get_shared_tasks():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    # Find tasks where current user is in sharedWith array
    shared_tasks = tasks_collection.find({
        'sharedWith': session['user_id']
    })

    tasks = []
    for task in shared_tasks:
        task['id'] = str(task['_id'])
        # Get owner's username
        owner = users_collection.find_one({'_id': ObjectId(task['user_id'])})
        task['owner'] = owner['username'] if owner else 'Unknown'
        del task['_id']
        tasks.append(task)

    return jsonify(tasks)


# Get user notifications
@app.route('/notifications')
def get_notifications():
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    notifications = list(notifications_collection.find({
        'user_id': session['user_id']
    }).sort('created_at', -1).limit(20))  # Last 20 notifications

    # Convert ObjectId to string
    for note in notifications:
        note['_id'] = str(note['_id'])
        note['created_at'] = note['created_at'].strftime('%Y-%m-%d %H:%M')

    return jsonify(notifications)


## Update the mark_notification_read function
@app.route('/notifications/<note_id>/read', methods=['POST'])
def mark_notification_read(note_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    try:
        result = notifications_collection.update_one(
            {'_id': ObjectId(note_id), 'user_id': session['user_id']},
            {'$set': {'is_read': True}}
        )

        return jsonify({'success': result.modified_count > 0})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Add this new endpoint for getting all notifications
@app.route('/notifications/all')
def get_all_notifications():
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    notifications = list(notifications_collection.find({
        'user_id': session['user_id']
    }).sort('created_at', -1).limit(50))  # Increased to 50

    # Convert ObjectId to string and format date
    for note in notifications:
        note['_id'] = str(note['_id'])
        if isinstance(note['created_at'], datetime):
            note['created_at'] = note['created_at'].strftime('%Y-%m-%d %H:%M')

    return jsonify(notifications)
@socketio.on('join')
def handle_join(data):
    if 'user_id' in session and data.get('userId') == session['user_id']:
        join_room(session['user_id'])
        emit('joined', {'message': f'Joined room for user {session["user_id"]}'})


# Analytics routes
@app.route('/analytics/overview')
def analytics_overview():
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    user_id = session['user_id']

    # Get basic stats with proper date comparison for overdue tasks
    total_tasks = tasks_collection.count_documents({'user_id': user_id})
    completed_tasks = tasks_collection.count_documents({
        'user_id': user_id,
        'status': 'Completed'
    })
    pending_tasks = tasks_collection.count_documents({
        'user_id': user_id,
        'status': 'Pending'
    })
    in_progress_tasks = tasks_collection.count_documents({
        'user_id': user_id,
        'status': 'In Progress'
    })

    # Calculate overdue tasks (deadline passed but not completed) - FIXED
    today = datetime.utcnow().date()
    overdue_tasks = tasks_collection.count_documents({
        'user_id': user_id,
        'deadline': {'$lt': today.isoformat()},
        'status': {'$nin': ['Completed']}  # Exclude completed tasks
    })

    # Calculate completion rate
    completion_rate = 0
    if total_tasks > 0:
        completion_rate = round((completed_tasks / total_tasks) * 100, 2)

    return jsonify({
        'total_tasks': total_tasks,
        'completed_tasks': completed_tasks,
        'pending_tasks': pending_tasks,
        'in_progress_tasks': in_progress_tasks,
        'overdue_tasks': overdue_tasks,
        'completion_rate': completion_rate
    })

@app.route('/analytics/trends')
def analytics_trends():
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    user_id = session['user_id']
    timeframe = request.args.get('timeframe', 'weekly')

    # Calculate start date based on timeframe
    if timeframe == 'weekly':
        start_date = datetime.utcnow() - timedelta(days=7)
    else:  # monthly
        start_date = datetime.utcnow() - timedelta(days=30)

    # Get completion trends - FIXED aggregation
    pipeline = [
        {
            '$match': {
                'user_id': user_id,
                'created_at': {'$gte': start_date}
            }
        },
        {
            '$group': {
                '_id': {
                    'year': {'$year': '$created_at'},
                    'month': {'$month': '$created_at'},
                    'day': {'$dayOfMonth': '$created_at'}
                },
                'date': {'$first': '$created_at'},
                'tasks_created': {'$sum': 1},
                'tasks_completed': {
                    '$sum': {
                        '$cond': [
                            {'$eq': ['$status', 'Completed']},
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {'$sort': {'date': 1}}  # Sort by date for proper ordering
    ]

    trends_data = list(tasks_collection.aggregate(pipeline))

    # Format the response with proper date handling
    trends = []
    for data in trends_data:
        date_str = data['date'].strftime('%Y-%m-%d')
        trends.append({
            'date': date_str,
            'tasks_created': data['tasks_created'],
            'tasks_completed': data['tasks_completed']
        })

    return jsonify({
        'timeframe': timeframe,
        'trends': trends
    })
@app.route('/analytics/status-distribution')
def status_distribution():
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    user_id = session['user_id']

    pipeline = [
        {'$match': {'user_id': user_id}},
        {'$group': {
            '_id': '$status',
            'count': {'$sum': 1}
        }}
    ]

    status_data = list(tasks_collection.aggregate(pipeline))

    distribution = {}
    for data in status_data:
        distribution[data['_id']] = data['count']

    return jsonify(distribution)


# Attachment routes
@app.route('/tasks/<task_id>/attachments', methods=['POST'])
def upload_attachment(task_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    user_id = session['user_id']

    # Check if task exists and belongs to user
    task = tasks_collection.find_one({'_id': ObjectId(task_id), 'user_id': user_id})
    if not task:
        return jsonify({'error': 'Task not found or unauthorized'}), 404

    # Check if file was uploaded
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']

    # Check if file was selected
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    # Validate file
    if file and allowed_file(file.filename):
        try:
            filename = secure_filename(file.filename)

            # Create user-specific directory
            user_upload_dir = os.path.join(app.config['UPLOAD_FOLDER'], user_id)
            os.makedirs(user_upload_dir, exist_ok=True)

            # Generate unique filename to prevent overwrites
            base, ext = os.path.splitext(filename)
            unique_filename = f"{base}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}{ext}"
            filepath = os.path.join(user_upload_dir, unique_filename)

            # Save file
            file.save(filepath)

            # Create attachment record
            attachment = {
                'filename': unique_filename,
                'original_name': filename,
                'uploaded_at': datetime.utcnow(),
                'size': os.path.getsize(filepath),
                'mimetype': file.mimetype
            }

            # Update task with attachment
            tasks_collection.update_one(
                {'_id': ObjectId(task_id)},
                {'$push': {'attachments': attachment}}
            )

            # Create notification
            create_notification(
                user_id=user_id,
                message=f"File '{filename}' uploaded to task '{task['title']}'",
                notification_type='info'
            )

            return jsonify({
                'message': 'File uploaded successfully',
                'attachment': attachment
            })

        except Exception as e:
            return jsonify({'error': f'Error uploading file: {str(e)}'}), 500

    return jsonify({'error': 'File type not allowed'}), 400


@app.route('/tasks/<task_id>/attachments', methods=['GET'])
def get_task_attachments(task_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    user_id = session['user_id']

    # Check if task exists and belongs to user
    task = tasks_collection.find_one({'_id': ObjectId(task_id), 'user_id': user_id})
    if not task:
        return jsonify({'error': 'Task not found or unauthorized'}), 404

    # Return attachments or empty array
    attachments = task.get('attachments', [])
    return jsonify(attachments)


@app.route('/tasks/<task_id>/attachments/<filename>', methods=['GET'])
def download_attachment(task_id, filename):
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    user_id = session['user_id']

    # Check if task exists and belongs to user
    task = tasks_collection.find_one({'_id': ObjectId(task_id), 'user_id': user_id})
    if not task:
        return jsonify({'error': 'Task not found or unauthorized'}), 404

    # Check if attachment exists
    attachment = None
    for att in task.get('attachments', []):
        if att['filename'] == filename:
            attachment = att
            break

    if not attachment:
        return jsonify({'error': 'Attachment not found'}), 404

    # Build file path
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], user_id, filename)

    # Check if file exists
    if not os.path.isfile(file_path):
        return jsonify({'error': 'File not found on server'}), 404

    # Send file
    return send_file(
        file_path,
        as_attachment=True,
        download_name=attachment['original_name']
    )


@app.route('/shared/tasks/<task_id>/attachments', methods=['GET'])
def get_shared_task_attachments(task_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    user_id = session['user_id']

    # Check if task is shared with current user
    task = tasks_collection.find_one({
        '_id': ObjectId(task_id),
        'sharedWith': user_id
    })

    if not task:
        return jsonify({'error': 'Task not found or not shared with you'}), 404

    # Return attachments or empty array
    attachments = task.get('attachments', [])
    return jsonify(attachments)


@app.route('/shared/tasks/<task_id>/attachments/<filename>', methods=['GET'])
def download_shared_attachment(task_id, filename):
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    user_id = session['user_id']

    # Check if task is shared with current user
    task = tasks_collection.find_one({
        '_id': ObjectId(task_id),
        'sharedWith': user_id
    })

    if not task:
        return jsonify({'error': 'Task not found or not shared with you'}), 404

    # Check if attachment exists
    attachment = None
    for att in task.get('attachments', []):
        if att['filename'] == filename:
            attachment = att
            break

    if not attachment:
        return jsonify({'error': 'Attachment not found'}), 404

    # Build file path (file is stored in owner's directory)
    owner_id = task['user_id']
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], owner_id, filename)

    # Check if file exists
    if not os.path.isfile(file_path):
        return jsonify({'error': 'File not found on server'}), 404

    # Send file
    return send_file(
        file_path,
        as_attachment=True,
        download_name=attachment['original_name']
    )

@app.route('/tasks/<task_id>/attachments/<filename>', methods=['DELETE'])
def delete_attachment(task_id, filename):
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    user_id = session['user_id']

    # Check if task exists and belongs to user
    task = tasks_collection.find_one({'_id': ObjectId(task_id), 'user_id': user_id})
    if not task:
        return jsonify({'error': 'Task not found or unauthorized'}), 404

    # Remove attachment from database
    result = tasks_collection.update_one(
        {'_id': ObjectId(task_id)},
        {'$pull': {'attachments': {'filename': filename}}}
    )

    if result.modified_count == 0:
        return jsonify({'error': 'Attachment not found'}), 404

    # Delete physical file
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], user_id, filename)
    if os.path.exists(file_path):
        os.remove(file_path)

    # Create notification
    create_notification(
        user_id=user_id,
        message=f"Attachment deleted from task '{task['title']}'",
        notification_type='warning'
    )

    return jsonify({'message': 'Attachment deleted successfully'})

# Mark all notifications as read for the current user
@app.route('/notifications/read_all', methods=['POST'])
def mark_all_notifications_read():
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    try:
        # Update all unread notifications for this user
        result = notifications_collection.update_many(
            {'user_id': session['user_id'], 'is_read': False},
            {'$set': {'is_read': True}}
        )
        # Emit a real-time event to refresh the UI for this user
        socketio.emit('notifications_updated', {'user_id': session['user_id']}, room=session['user_id'])

        return jsonify({'message': f'Marked {result.modified_count} notifications as read', 'modified_count': result.modified_count})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Delete all notifications for the current user
@app.route('/notifications/delete_all', methods=['DELETE'])
def delete_all_notifications():
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    try:
        # Delete all notifications for this user
        result = notifications_collection.delete_many({'user_id': session['user_id']})
        # Emit a real-time event to refresh the UI for this user
        socketio.emit('notifications_updated', {'user_id': session['user_id']}, room=session['user_id'])

        return jsonify({'message': f'Deleted {result.deleted_count} notifications', 'deleted_count': result.deleted_count})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Delete a single notification
@app.route('/notifications/<note_id>', methods=['DELETE'])
def delete_single_notification(note_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    try:
        # Delete the notification if it belongs to the current user
        result = notifications_collection.delete_one({'_id': ObjectId(note_id), 'user_id': session['user_id']})
        if result.deleted_count == 0:
            return jsonify({'error': 'Notification not found or unauthorized'}), 404

        # Emit a real-time event to refresh the UI for this user
        socketio.emit('notifications_updated', {'user_id': session['user_id']}, room=session['user_id'])

        return jsonify({'message': 'Notification deleted successfully'})

    except Exception as e:
        return jsonify({'error': str(e)}), 500
# Remove a shared task from user's view (doesn't delete the actual task)
@app.route('/tasks/shared/<task_id>/remove', methods=['DELETE'])
def remove_shared_task(task_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    user_id = session['user_id']

    try:
        # Remove current user from the sharedWith array of the task
        result = tasks_collection.update_one(
            {'_id': ObjectId(task_id)},
            {'$pull': {'sharedWith': user_id}}
        )

        if result.modified_count == 0:
            return jsonify({'error': 'Task not found or not shared with you'}), 404

        # Create notification for the task owner (optional)
        task = tasks_collection.find_one({'_id': ObjectId(task_id)})
        if task:
            create_notification(
                user_id=task['user_id'],
                message=f"{session['username']} removed your shared task '{task['title']}' from their list",
                notification_type='info'
            )

        return jsonify({'message': 'Task removed from your shared list'})

    except Exception as e:
        return jsonify({'error': str(e)}), 500
@app.route('/delete_account', methods=['POST'])
def delete_account():
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    user_id = session['user_id']
    password = request.form['password']

    user = users_collection.find_one({'_id': ObjectId(user_id)})

    if user and check_password_hash(user['password'], password):
        users_collection.delete_one({'_id': ObjectId(user_id)})
        tasks_collection.delete_many({'user_id': user_id})
        session.clear()
        return jsonify({'message': 'Your account has been permanently deleted'}), 200
    else:
        return jsonify({'error': 'Wrong password. Your account was not deleted'}), 400

# Update your Socket.IO initialization
socketio = SocketIO(app,
                   cors_allowed_origins="*",
                   async_mode='threading',
                   logger=True,
                   engineio_logger=False)
if __name__ == '__main__':
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True)

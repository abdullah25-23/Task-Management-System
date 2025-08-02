from flask import   jsonify,flash
from pymongo import MongoClient
from datetime import datetime
from bson.objectid import ObjectId
from flask import Flask, render_template, request, redirect, session, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import timedelta

app = Flask(__name__)
app.permanent_session_lifetime = timedelta(hours=1)  # Session expires after 1 hour
pp.secret_key = ''  # <== 🔥 Place this here
# MongoDB Connection
# Enter your mango db Connection String
client = MongoClient("mongodb+srv://user:password@taskcluster.zlltsdv.mongodb.net/?retryWrites=true&w=majority&appName=TaskCluster")

db = client['task_manager']
tasks_collection = db['tasks']
users_collection = db['users']


@app.route('/')
def index():
    if 'username' not in session:
        return redirect(url_for('login'))
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']

    # Only get status filter from URL, not session
    status_filter = request.args.get('status')
    search_query = request.args.get('search')
    sort_by_deadline = request.args.get('sort') == 'deadline'

    # Get counts for ALL tasks
    total_count = tasks_collection.count_documents({'user_id': user_id})
    Pending_count = tasks_collection.count_documents({'user_id': user_id, 'status': 'Pending'})
    In_Progress_count = tasks_collection.count_documents({'user_id': user_id, 'status': 'In Progress'})
    Completed_count = tasks_collection.count_documents({'user_id': user_id, 'status': 'Completed'})

    tasks = []
    show_components = bool(status_filter)  # Only show if filter is explicitly selected

    if status_filter:
        query = {"user_id": user_id}
        if status_filter != 'all':
            query['status'] = status_filter
        if search_query:
            query['title'] = {'$regex': search_query, '$options': 'i'}

        if sort_by_deadline:
            tasks_cursor = tasks_collection.find(query).sort('deadline', 1)
        else:
            tasks_cursor = tasks_collection.find(query)

        for task in tasks_cursor:
            task['id'] = str(task['_id'])
            del task['_id']
            tasks.append(task)

    # Calculate overall completion percentage
    overall_completed_percentage = 0
    if total_count > 0:
        overall_completed_percentage = round((Completed_count / total_count) * 100)

    return render_template('index.html',
                           tasks=tasks,
                           selected_status=status_filter,  # Will be None on first load
                           total_count=total_count,
                           Pending_count=Pending_count,
                           In_Progress_count=In_Progress_count,
                           Completed_count=Completed_count,
                           overall_completed_percentage=overall_completed_percentage,
                           username=session.get('username'),
                           show_table=show_components,
                           show_progress=show_components)

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


@app.route('/delete_task/<task_id>')
def delete_task(task_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']
    result = tasks_collection.delete_one({'_id': ObjectId(task_id), 'user_id': user_id})
    if result.deleted_count == 0:
        return "Unauthorized or task not found", 403
    if 'last_filter' in session:
        return redirect(url_for('index', status=session['last_filter']))
    return redirect(url_for('index'))


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

    if 'last_filter' in session:
        return redirect(url_for('index', status=session['last_filter']))
    return redirect(url_for('index'))

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
        'user_id': user_id
    }

    tasks_collection.insert_one(task)
    flash('Task added successfully!', 'success')
    if 'last_filter' in session:
        return redirect(url_for('index', status=session['last_filter']))
    return redirect(url_for('index'))

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


if __name__ == '__main__':
    app.run(debug=True)


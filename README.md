# EchoArchive

Community memory preservation and digital archiving platform, in the same structure as the minishop project:
one Django project (`echoarchive`), one app (`echoarchiveweb`), plain `mysql.connector` queries in `views.py`,
a flat `templates` folder, and a `static` folder for css and images.

## Folder layout

```
echoarchive/
  manage.py
  echoarchive.sql            MySQL tables and demo data (run this first)
  echoarchive/               settings.py, urls.py, wsgi.py
  echoarchiveweb/
    views.py   urls.py
    templates/               29 html files, no sub folders, no base template
    static/css/style.css
    static/images/           logo.png, favicon.png
    static/images/uploads/   pictures of memories, places and scanned documents
```

## Run it

1. `pip install django mysql-connector-python Pillow pytesseract`
2. `mysql -u root -p < echoarchive.sql`  (connection details are in `connect()` at the top of `views.py`)
3. Install Tesseract with Malayalam (`sudo apt install tesseract-ocr tesseract-ocr-mal`; on Windows set `TESSERACT_CMD` in `settings.py`)
4. `python manage.py runserver` and open http://127.0.0.1:8000/

No `migrate` is needed. Passwords are stored as plain text.

## Logins

Every username is the first name. Every password is the first name followed by 123. The same table is on the login page.

| Role | Name | Username | Password | Where |
|---|---|---|---|---|
| Super admin | Anil Kumar | anil | anil123 | whole platform |
| Community admin | Meera Nair | meera | meera123 | Palakkad Heritage, Kerala Kitchen |
| Community admin | Suresh Babu | suresh | suresh123 | Kerala Folk Traditions |
| Community admin | Lakshmi Devi | lakshmi | lakshmi123 | College Memories |
| Moderator | Arun Menon | arun | arun123 | Palakkad Heritage, Kerala Kitchen |
| Moderator | Divya Raj | divya | divya123 | Kerala Folk Traditions |
| Moderator | Rahul Varma | rahul | rahul123 | College Memories |
| Contributor | Latha Krishnan | latha | latha123 | Palakkad Heritage, Folk Traditions, Kerala Kitchen |
| Contributor | Vishnu Prasad | vishnu | vishnu123 | Palakkad Heritage |
| Contributor | Anjali Mohan | anjali | anjali123 | Folk Traditions, College Memories |
| Contributor | Gopalan Nair | gopalan | gopalan123 | Palakkad Heritage, Kerala Kitchen |
| Registered user | Nikhil Das | nikhil | nikhil123 | asked to join Palakkad Heritage |
| Registered user | Sreya George | sreya | sreya123 | not in any community |

## Who can do what

* **Visitor** (no login): browse and search approved memories in public communities.
* **Registered user**: can ask to join a community. Becomes a contributor once its admin approves.
* **Contributor**: the only role that adds memories. Adds details, images of the memory, images of the place,
  and scanned documents (text is read with Tesseract and checked by the contributor). Can also suggest corrections and report a memory.
* **Moderator**: reviews submissions, approves or rejects with a reason, asks for changes, links related and conflicting accounts.
* **Community admin**: approves join requests, assigns moderators, manages members, categories and community details.
* **Super admin**: does not need to join any community. Can open and manage every community, review any submission,
  manage users, categories, reports and activity. In Categories, click a number to see the memories in that category.

## Testing

See `TESTING.md` for how to test OCR text extraction and duplicate entries (sample scans are in `test_files/`, ready-made duplicate entries are in `test_duplicate.sql`).

## Database tables

users, communities, memberships, categories, subjects, memories, media, ocr, links, verifications,
corrections, versions, notifications, reports, activity.

New details: users have a place; communities have a place; memories have a district, a landmark near the place,
the people involved and how the contributor knows it; each image has a kind (memory image, location image,
scanned document) and a caption.

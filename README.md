### Intro
Beekeeper is a lightweight web app designed to allow you to do AI training on a remote server as part of your home lab. At its core, it's designed to handle:

1. Cloning a repository
2. Setting up the python environment (based on your requirements.txt)
3. Remote log streaming
4. Tensorboard display
5. File downloads
6. Optional user authentication and API keys


### Setup
git clone https://github.com/bobcowher/beekeeper.git 
cd beekeeper 
bash setup.sh

*Note - This product has been tested on Ubuntu only, so far. 


### Authentication

Beekeeper includes **optional authentication** (disabled by default for backward compatibility):
- Email/password authentication with bcrypt hashing
- Admin panel for user management
- API key system for programmatic access
- Session-based web authentication

See [AUTH_README.md](AUTH_README.md) for complete authentication documentation.

### Current Limitations

1. **GitHub auth** - Beekeeper has no method of authenticating with your remote repo. It only works on repos you've made public.
2. **HTTPS** - For HTTPS, you'll need to put Beekeeper behind a reverse proxy (nginx, Caddy, etc).
3. **Multi-server support** - This is a single-server product. Multi-server coordination is not currently supported.

For more information, visit the site below - 

https://www.teaandrobots.com/software/beekeeper/


### 1.0 Release Notes/Initial Software Specification

Non-functional requirements:
- Should be written in Python with Flask
- Should be light weight & performant
- Code should be simple, with clear structure, wherever possible. 
- The application will set its home directory to its install/checkout location. We will refer to this as BEEKEEPER_HOME. 

Theme:
- Designed to be a yellow VSCode spinoff. Simple, clear structure, with minimal logos. 

Core Functional Requirements:
- Users should be able to create Projects in the UI. A project will require 
    - A project name(no spaces)
    - A Git url
    - Default branch (default should be main)
    - A target Python version(dropdown)
    - The python file for training (default should be train.py)
    - The tensorboard log dir (default should be "runs")
    - Pip requirements file (default should be requirements.txt)
- Once the project is created, it should create a folder for the project under $BEEKEEPER_HOME/projects/$projectname
- Code will be checked out to $BEEKEEPER_HOME/projects/$projectname/src
- A virtual environment should be created for the project to run in. 
- Clicking on a project in the dashboard should result in going to a page for that project, with a clear back button. 
- Once a project is created, the user should have the ability to run or stop the project from the project page. 
- While the project is running, the user should have the option to see logs on the project page. 
- The user should have the option to see Tensorboard results for a project, in realtime, on the project page. 
- On the main page, the user should have the option to see the current GPU, CPU, and memory statistics for the host.

Things for later:
- We'll eventually need to figure out logins and security. For V1, we don't care. 
- Auth to GitHub. For now, all GitHub projects used will be public. 

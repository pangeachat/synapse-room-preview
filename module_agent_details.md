# Synapse Room Preview Module - Agent Documentation

## Project Overview

**Name:** synapse-room-preview  
**Type:** Matrix Synapse Module  
**Purpose:** Enable authenticated users to read content of pre-configured state events from Matrix rooms without being a member of those rooms.

## Architecture

### Core Components

1. **SynapseRoomPreview** - Main module class that integrates with Synapse
2. **SynapseRoomPreviewConfig** - Configuration management using attrs
3. **Module API Integration** - Uses Synapse's ModuleApi for server integration

### Technology Stack

- **Language:** Python 3.8+
- **Framework:** Matrix Synapse Module API
- **Dependencies:**
  - `attrs` - Configuration management
  - `matrix-synapse` - Core Synapse integration
- **Development Tools:**
  - `mypy` (1.6.1) - Type checking
  - `black` (23.10.0) - Code formatting  
  - `ruff` (0.1.1) - Linting
  - `tox` - Testing automation
  - `twisted` - Async framework testing

## Module Functionality

### Current Implementation Status
- **Configuration parsing** - Basic structure implemented
- **Module initialization** - Core setup complete
- **State event reading** - **TODO: Implementation pending**

### Key Features (Planned)
- Allow reading specific state events from rooms
- Authentication-based access control
- Configurable event type filtering
- Non-member room content access

## Development Environment

### Setup
```bash
# Activate virtual environment
source .venv/bin/activate

# Install in development mode
pip install -e .[dev]

# Run tests
tox -e py
# or
trial tests

# View test logs (for debugging)
tail -f synapse.log

# Linting and type checking
./scripts-dev/lint.sh
```

### Code Quality Standards
- **Type checking:** Strict mypy enforcement
- **Code style:** Black formatter (88 char line length)
- **Linting:** Ruff with comprehensive rule set
- **Testing:** Twisted trial framework

## Configuration

### Synapse Integration
```yaml
modules:
  - module: synapse_room_preview.SynapseRoomPreview
    config:
      # TODO: Configuration options to be defined
```

### Development Configuration
- **Line length:** 88 characters
- **Python version:** ≥3.8
- **License:** Apache Software License

## File Structure

```
synapse-room-preview/
├── synapse_room_preview/          # Main module package
│   ├── __init__.py               # Core module implementation
│   └── py.typed                  # Type hint marker
├── tests/                        # Test suite
│   ├── __init__.py
│   └── test_example.py
├── scripts-dev/                  # Development scripts
│   └── lint.sh                   # Linting automation
├── .github/workflows/            # CI/CD
│   └── ci.yml
├── pyproject.toml               # Project configuration
├── tox.ini                      # Test automation
├── README.md                    # User documentation
└── .gitignore                   # Git exclusions
```

## Development Workflow

### Testing Strategy
- **Unit tests** via `tox -e py` or `trial tests`
- **Type checking** via `mypy`
- **Code formatting** via `black`
- **Linting** via `ruff`

### Release Process
1. Update version in configuration
2. Stage and commit changes
3. Create signed git tag
4. Push to repository
5. Create GitHub/GitLab release
6. Build and upload to PyPI

## Current Development Status

### Completed
- ✅ Project scaffolding and structure
- ✅ Basic module class with Synapse integration
- ✅ Configuration parsing framework
- ✅ Development environment setup
- ✅ CI/CD pipeline configuration

### Pending Implementation
- ❌ State event reading logic
- ❌ Room access control mechanisms
- ❌ Event type filtering
- ❌ Authentication verification
- ❌ Error handling and logging
- ❌ Configuration options definition
- ❌ Comprehensive test suite

## Integration Points

### Synapse Module API
- **ModuleApi** - Primary integration interface
- **Configuration parsing** - Module config validation
- **Room state access** - Core functionality target

### Future Considerations
- Performance optimization for large deployments
- Security implications of non-member access
- Rate limiting and abuse prevention
- Audit logging capabilities

## Repository Information

- **Repository:** https://github.com/pangeachat/synapse-room-preview
- **Branch:** main
- **License:** Apache Software License
- **Python Requirements:** ≥3.8

## Next Steps for Development

1. **Define configuration schema** - Specify which state events can be accessed
2. **Implement core functionality** - Room state reading without membership
3. **Add comprehensive tests** - Cover all access scenarios
4. **Security review** - Ensure proper authentication and authorization
5. **Performance testing** - Validate scalability with large room counts
6. **Documentation completion** - Full API and configuration docs

## Workflow

- **Activate virtual environment first:** Run `source .venv/bin/activate` before executing any commands.
- Run `tox -e py` to execute end-to-end tests for every feature, ensuring all tests are covered.
- View test logs (for debugging) by running `tail -f synapse.log` during or after test execution.
- Ensure code formatting meets standards by running `./scripts-dev/lint.sh` and iterating until no errors are reported.
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import SQLModel, Field, create_engine, Session, select
from typing import List, Optional
from pydantic import BaseModel
import datetime
import logging
import os
from contextlib import asynccontextmanager

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Models ---
class Employee(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    monthly_salary: float

class PayrollRecord(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    employee_id: int
    employee_name: str
    month_year: str # e.g., "April 2026"
    total_days: int
    worked_days: int
    basic: float
    hra: float
    special: float
    transport: float
    medical: float
    net_salary: float
    created_at: datetime.datetime = Field(default_factory=datetime.datetime.utcnow)

# --- Schemas ---
class PayrollCalculateRequest(BaseModel):
    employee_id: int
    total_days: int = 30
    worked_days: int
    month_year: str

# --- Database Setup ---
# Support common production DB variables (Vercel Postgres, Supabase, etc.)
DATABASE_URL = (
    os.environ.get("POSTGRES_URL") or 
    os.environ.get("DATABASE_URL") or 
    os.environ.get("SUPABASE_URL") or 
    "sqlite:///payroll.db"
)

# Fix for Postgres variants (SQLAlchemy requires postgresql:// instead of postgres://)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})



def create_db_and_tables():
    SQLModel.metadata.create_all(engine)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic
    logger.info("Initializing database...")
    create_db_and_tables()
    yield
    # Shutdown logic
    logger.info("Shutting down...")

def get_session():
    with Session(engine) as session:
        yield session

# --- FastAPI App ---
app = FastAPI(title="Monarch Payroll System", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    # Use absolute path for Vercel stability
    path = os.path.join(os.path.dirname(__file__), "index.html")
    return FileResponse(path)

# --- Routes: Employees ---

@app.post("/employees/", response_model=Employee)
def create_employee(employee: Employee, session: Session = Depends(get_session)):
    logger.info(f"Adding employee: {employee.name}")
    session.add(employee)
    session.commit()
    session.refresh(employee)
    return employee

@app.get("/employees/", response_model=List[Employee])
def read_employees(session: Session = Depends(get_session)):
    return session.exec(select(Employee)).all()

@app.put("/employees/{employee_id}", response_model=Employee)
def update_employee(employee_id: int, employee_data: Employee, session: Session = Depends(get_session)):
    db_employee = session.get(Employee, employee_id)
    if not db_employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    db_employee.name = employee_data.name
    db_employee.monthly_salary = employee_data.monthly_salary
    session.add(db_employee)
    session.commit()
    session.refresh(db_employee)
    return db_employee

@app.delete("/employees/{employee_id}")
def delete_employee(employee_id: int, session: Session = Depends(get_session)):
    employee = session.get(Employee, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    session.delete(employee)
    session.commit()
    return {"ok": True}

# --- Routes: Payroll ---

@app.post("/payroll/calculate", response_model=PayrollRecord)
def calculate_payroll(req: PayrollCalculateRequest, session: Session = Depends(get_session)):
    employee = session.get(Employee, req.employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    # Logic provided by user
    payable_ratio = req.worked_days / req.total_days
    
    basic = employee.monthly_salary * 0.40 * payable_ratio
    hra = employee.monthly_salary * 0.20 * payable_ratio
    special = employee.monthly_salary * 0.25 * payable_ratio
    transport = employee.monthly_salary * 0.10 * payable_ratio
    medical = employee.monthly_salary * 0.05 * payable_ratio
    net_salary = employee.monthly_salary * payable_ratio
    
    record = PayrollRecord(
        employee_id=employee.id,
        employee_name=employee.name,
        month_year=req.month_year,
        total_days=req.total_days,
        worked_days=req.worked_days,
        basic=round(basic, 2),
        hra=round(hra, 2),
        special=round(special, 2),
        transport=round(transport, 2),
        medical=round(medical, 2),
        net_salary=round(net_salary, 2)
    )
    
    session.add(record)
    session.commit()
    session.refresh(record)
    return record

@app.get("/payroll/history", response_model=List[PayrollRecord])
def get_payroll_history(session: Session = Depends(get_session)):
    return session.exec(select(PayrollRecord).order_by(PayrollRecord.created_at.desc())).all()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

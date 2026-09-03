import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import LoginPage from './pages/LoginPage'
import DashboardLayout from './components/DashboardLayout'
import OverviewPage from './pages/OverviewPage'
import ReviewsPage from './pages/ReviewsPage'
import ReviewDetailPage from './pages/ReviewDetailPage'
import ConfigPage from './pages/ConfigPage'
import OpsPage from './pages/OpsPage'
import PlaygroundPage from './pages/PlaygroundPage'
import { AuthProvider, useAuth } from './lib/auth'

export default function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/dashboard/login" element={<LoginPage />} />
        <Route path="/dashboard" element={<RequireAuth><DashboardLayout /></RequireAuth>}>
          <Route index element={<OverviewPage />} />
          <Route path="reviews" element={<ReviewsPage />} />
          <Route path="reviews/:id" element={<ReviewDetailPage />} />
          <Route path="config" element={<ConfigPage />} />
          <Route path="ops" element={<OpsPage />} />
          <Route path="playground" element={<PlaygroundPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/dashboard" replace />} />
      </Routes>
    </AuthProvider>
  )
}

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { authenticated, loading } = useAuth()
  const location = useLocation()
  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="skeleton h-10 w-10 rounded-full" />
      </div>
    )
  }
  if (!authenticated) {
    return <Navigate to="/dashboard/login" state={{ from: location }} replace />
  }
  return <>{children}</>
}

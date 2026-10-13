import { ApiError } from '../api/client.js'

/** Abort checks also suppress late results from dependencies that ignore abort. */
export async function readResource(load, signal, publish) {
  try {
    const data = await load(signal)
    if (!signal.aborted) publish({ status: 'success', data, error: null })
  } catch (error) {
    if (!signal.aborted) publish({ status: 'error', data: null,
      error: error instanceof ApiError ? error.message : 'Unable to load this view. Please try again.' })
  }
}

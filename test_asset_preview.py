"""Read-only authenticated reference previews, independent of generation."""
import unittest
import test_asset_library as library
from test_asset_library import image_bytes
from asset_storage import resolve_asset_path

class AssetPreviewTests(unittest.TestCase):
    setUp=library.LibraryApiTests.setUp
    tearDown=library.LibraryApiTests.tearDown
    create_scene=library.LibraryApiTests.create_scene
    upload=library.LibraryApiTests.upload
    def test_preview_all_formats_readonly(self):
        for name,fmt,mime in [('jpg','JPEG','image/jpeg'),('png','PNG','image/png'),('webp','WEBP','image/webp')]:
            data=image_bytes(fmt)
            asset=self.upload(name=name,filename='ref.'+name,data=data).json()
            before=self.store.get_asset(asset['id'])
            response=self.http.get('/api/admin/assets/'+asset['id']+'/image')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.content,data)
            self.assertEqual(response.headers['content-type'],mime)
            self.assertEqual(response.headers['cache-control'],'private, no-store')
            self.assertEqual(self.store.get_asset(asset['id']),before)
        self.assertEqual(self.queued,[])
    def test_preview_requires_admin(self):
        asset=self.upload().json()
        response=self.http.get('/api/admin/assets/'+asset['id']+'/image',headers={'X-Admin-Key':'wrong'})
        self.assertEqual(response.status_code,401)
    def test_unknown_or_missing_image(self):
        self.assertEqual(self.http.get('/api/admin/assets/missing/image').status_code,404)
        asset=self.upload().json();resolve_asset_path(asset['file_path']).unlink()
        self.assertEqual(self.http.get('/api/admin/assets/'+asset['id']+'/image').status_code,404)
    def test_tampered_paths_do_not_expose_files(self):
        asset=self.upload().json()
        for path in ['../outside.png','C:/Windows/win.ini',asset['project_id']+'/other.png',asset['project_id']+'/'+asset['id']+'.svg']:
            self.store._conn.execute('UPDATE assets SET file_path=? WHERE id=?',(path,asset['id']));self.store._conn.commit()
            response=self.http.get('/api/admin/assets/'+asset['id']+'/image')
            self.assertEqual(response.status_code,404,response.text)

if __name__=='__main__':unittest.main()
